"""Статьи расходов Solana-связки (ТЗ §5.1, §13; SOLANA_ROUTERS §3, §5): одна модель для кандидата маршрута и для чека.

Правила — каждое против своей денежной ошибки:
- сумма статьи — сырой int в единицах своего актива (лампорты, raw токена); неизвестная сумма — None, не 0 (R05);
- included_in_input_output=True — статья уже внутри потока токенов (платформа Jupiter, пулы, налог): показываем
  для объяснения, второй раз не вычитаем (R04);
- payer — чей кошелёк платит. Статья спонсора (gasless, чужой payer) не наш расход (§5.1); неизвестный payer —
  неизвестная стоимость, а не «бесплатно»;
- rent_deposit / rent_refund — возвратный депозит: заблокированный капитал. Идёт в запас SOL и cash-outlay, но не в
  экономическую стоимость и не в безвозвратные комиссии (S18). Невозвратный rent — отдельная статья;
- meta.fee чека уже содержит base+priority: оценку priority к нему не прибавлять (S17); tip — отдельный перевод;
- OKX tradeFee — оценка сети в USD. Если у кандидата есть своя оценка в лампортах, tradeFee помечается superseded
  и в сумму не идёт (§5.1: «не складывать повторно»).
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, localcontext, ROUND_CEILING
from typing import Iterable, Mapping, Sequence

D = Decimal
NATIVE_SOL = "native:solana"         # лампорты, 9 знаков. Не mint: wSOL (So111…112) — отдельный актив токен-счёта
SOL_DECIMALS = 9
USD = "USD"                          # оценки провайдера в долларах (OKX tradeFee): отдельный актив, 1 USD ≠ 1 USDC
LAMPORTS_PER_SIGNATURE = 5000        # протокол Solana: базовая плата за подпись (не денежный лимит)
MICRO_LAMPORTS_PER_LAMPORT = 1_000_000
MAX_TX_COMPUTE_UNITS = 1_400_000     # протокольный потолок CU на транзакцию

KINDS = frozenset({
    "network_base",           # 5000 × подписи (оценка кандидата)
    "network_priority",       # ceil(цена CU × ЗАПРОШЕННЫЙ лимит / 10^6)
    "network_total",          # meta.fee чека: base+priority уже внутри
    "tip",                    # отдельный перевод SOL (Jito/landing) — не входит в meta.fee
    "rent_deposit",           # возвратный депозит созданного счёта
    "rent_refund",            # возврат депозита при закрытии (кредит)
    "rent_nonrefundable",     # rent чужого/невозвратного счёта — настоящий расход
    "platform",               # комиссия платформы (Jupiter Order feeBps) — обычно внутри out
    "router",                 # комиссия роутера/агрегатора
    "dex_embedded",           # комиссии пулов внутри котировки
    "transfer_tax",           # Token-2022 transfer fee / налог токена
    "network_estimate_usd",   # OKX tradeFee: оценка сети в USD
    "perp_fee",               # комиссия перпа (пара, не спот)
})
REFUNDABLE_KINDS = frozenset({"rent_deposit", "rent_refund"})


class FeeError(ValueError):
    """Нарушен контракт статьи (float, отрицательная сумма, неизвестный вид) — ошибка кода, а не рынка."""


def _raw(x, what: str) -> int | None:
    if x is None:
        return None
    if isinstance(x, bool) or not isinstance(x, int):
        raise FeeError(f"{what}: сырой int или None, а не {type(x).__name__}")
    if x < 0:
        raise FeeError(f"{what}: отрицательная сумма {x}")
    return x


def human(raw: int, decimals: int) -> D:
    """raw → человеческие единицы точно (scaleb без float и без округления контекста)."""
    _raw(raw, "raw")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 30:
        raise FeeError(f"decimals вне 0..30: {decimals!r}")
    with localcontext() as c:
        c.prec = 80
        return D(raw).scaleb(-decimals)


def raw_of(amount: D, decimals: int) -> int:
    """Человеческие единицы → raw; дробный остаток ниже 1 raw — ошибка, а не молчаливое округление."""
    if isinstance(amount, (float, bool)) or not isinstance(amount, (D, int)):
        raise FeeError(f"сумма: Decimal/int, а не {type(amount).__name__}")
    with localcontext() as c:
        c.prec = 80
        v = D(amount).scaleb(decimals)
        if v != v.to_integral_value() or v < 0:
            raise FeeError(f"{amount} не выражается целым raw при {decimals} знаках")
        return int(v)


def dec_str(s, what: str) -> D:
    """Decimal из строки/числа API (tradeFee, impact): float не принимаем, мусор — ошибка."""
    if isinstance(s, (float, bool)) or s is None:
        raise FeeError(f"{what}: нужна строка-число, а не {type(s).__name__}")
    try:
        d = D(str(s).strip())
    except InvalidOperation:
        raise FeeError(f"{what}: не число ({str(s)[:40]!r})") from None
    if not d.is_finite():
        raise FeeError(f"{what}: не конечное число")
    return d


@dataclass(frozen=True)
class FeeComponent:
    kind: str
    asset: str                      # NATIVE_SOL | mint base58 | USD
    decimals: int
    amount_raw: int | None          # None — неизвестно
    payer: str | None               # base58 плательщика; None — неизвестен
    included_in_input_output: bool
    estimated: bool
    refundable: bool = False
    superseded: bool = False        # оценка заменена более точной (OKX tradeFee при своей оценке в лампортах)
    source: str = ""
    note: str = ""
    recipient: str | None = None    # получатель перевода (tip): allowlist сверяет роутер; None — не разобран

    def __post_init__(self):
        if self.kind not in KINDS:
            raise FeeError(f"неизвестный вид статьи {self.kind!r}")
        _raw(self.amount_raw, self.kind)
        if isinstance(self.decimals, bool) or not isinstance(self.decimals, int) or not 0 <= self.decimals <= 30:
            raise FeeError(f"{self.kind}: decimals {self.decimals!r}")
        if self.refundable != (self.kind in REFUNDABLE_KINDS):
            raise FeeError(f"{self.kind}: refundable={self.refundable} не соответствует виду")

    def human(self) -> D | None:
        return None if self.amount_raw is None else human(self.amount_raw, self.decimals)

    def as_record(self) -> dict:
        """Для журнала кандидатов/fee_events: суммы строками, без float."""
        return dict(kind=self.kind, asset=self.asset, decimals=self.decimals,
                    amount_raw=None if self.amount_raw is None else str(self.amount_raw), payer=self.payer,
                    included=self.included_in_input_output, estimated=self.estimated, refundable=self.refundable,
                    superseded=self.superseded, source=self.source, note=self.note, recipient=self.recipient)


def lamports(kind: str, amount: int | None, payer: str | None, *, estimated: bool, source: str,
             note: str = "", recipient: str | None = None) -> FeeComponent:
    return FeeComponent(kind=kind, asset=NATIVE_SOL, decimals=SOL_DECIMALS, amount_raw=amount, payer=payer,
                        included_in_input_output=False, estimated=estimated,
                        refundable=kind in REFUNDABLE_KINDS, source=source, note=note, recipient=recipient)


def priority_fee_lamports(cu_limit: int, cu_price_micro: int) -> int:
    """Priority fee Solana: ceil(цена CU в микролампортах × ЗАПРОШЕННЫЙ лимит / 10^6) — от лимита, не от расхода."""
    _raw(cu_limit, "cu_limit"); _raw(cu_price_micro, "cu_price")
    return -(-cu_limit * cu_price_micro // MICRO_LAMPORTS_PER_LAMPORT)


def network_components(*, n_signatures: int, cu_limit: int | None, cu_price_micro: int | None, payer: str | None,
                       source: str, limit_is_upper_bound: bool = False) -> tuple[FeeComponent, FeeComponent]:
    """Оценка сети кандидата: base (подписи) + priority. Цена CU неизвестна или лимит неизвестен при ненулевой
    цене — priority None (неизвестно), а не 0. Нет инструкции цены — цена 0, priority 0 (это известно)."""
    if isinstance(n_signatures, bool) or not isinstance(n_signatures, int) or n_signatures < 1:
        raise FeeError(f"подписей должно быть ≥ 1: {n_signatures!r}")
    base = lamports("network_base", LAMPORTS_PER_SIGNATURE * n_signatures, payer, estimated=False, source=source)
    if cu_price_micro is None:
        prio = lamports("network_priority", None, payer, estimated=True, source=source, note="цена CU неизвестна")
    elif cu_price_micro == 0:
        prio = lamports("network_priority", 0, payer, estimated=False, source=source)
    elif cu_limit is None:
        prio = lamports("network_priority", None, payer, estimated=True, source=source, note="лимит CU неизвестен")
    else:
        prio = lamports("network_priority", priority_fee_lamports(cu_limit, cu_price_micro), payer,
                        estimated=limit_is_upper_bound, source=source,
                        note="верхняя граница до симуляции" if limit_is_upper_bound else "")
    return base, prio


def receipt_components(*, fee_lamports: int, fee_payer: str, tip_lamports: int | None = 0, tip_payer: str | None = None,
                       rent_deposits: Sequence[int] = (), rent_refunds: Sequence[int] = (),
                       rent_nonrefundable: int = 0, source: str = "receipt") -> tuple[FeeComponent, ...]:
    """Статьи из разобранного чека. meta.fee — одна статья network_total: base+priority уже внутри, оценку priority
    не добавляем (S17). tip — отдельный перевод; None — перевод был, сумма не разобрана (неизвестно)."""
    out = [lamports("network_total", fee_lamports, fee_payer, estimated=False, source=source, note="meta.fee")]
    if tip_lamports is None or tip_lamports:
        out.append(lamports("tip", tip_lamports, tip_payer or fee_payer, estimated=False, source=source))
    out += [lamports("rent_deposit", a, fee_payer, estimated=False, source=source) for a in rent_deposits]
    out += [lamports("rent_refund", a, fee_payer, estimated=False, source=source) for a in rent_refunds]
    if rent_nonrefundable:
        out.append(lamports("rent_nonrefundable", rent_nonrefundable, fee_payer, estimated=False, source=source))
    return tuple(out)


def legacy_evm(gas_used: int, gas_price_wei: int, payer: str, native: str = "native:bsc") -> FeeComponent:
    """Газ BSC-сделки той же моделью (для общего отчёта): gas_used × цена — точная нативная сумма, 18 знаков."""
    return FeeComponent(kind="network_total", asset=native, decimals=18, amount_raw=_raw(gas_used, "gas") *
                        _raw(gas_price_wei, "gas_price"), payer=payer, included_in_input_output=False,
                        estimated=False, source="evm_receipt")


# --- денежная сторона -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PriceObs:
    """Цена 1 человеческой единицы asset в единицах unit (SOL в USDC и т.п.); время — монотонное, источник — строкой."""
    asset: str
    unit: str
    price: D
    observed_mono: float
    source: str

    def __post_init__(self):
        if not isinstance(self.price, D) or not self.price.is_finite() or self.price <= 0:
            raise FeeError(f"цена {self.asset}: положительный Decimal, а не {self.price!r}")


@dataclass(frozen=True)
class Valuation:
    """Сумма НАШИХ внешних расходов в unit. total None — есть неизвестное (unknown непуст): не 0 и не «дёшево»."""
    unit: str
    total: D | None
    parts: tuple[tuple[str, D], ...]
    unknown: tuple[str, ...]
    sponsored: tuple[str, ...]       # статьи чужого плательщика: видны, но не наш расход
    unchecked: tuple[str, ...] = ()  # проверки, для которых нет лимита владельца (возраст цены)


def own_external(components: Iterable[FeeComponent], wallet: str) -> list[FeeComponent]:
    """Наш расход сверх потоков токенов: не included, не возвратный, не superseded, плательщик — наш кошелёк или
    неизвестен (неизвестный плательщик = неизвестная стоимость, решает valuation)."""
    return [c for c in components if not c.included_in_input_output and not c.refundable and not c.superseded
            and (c.payer is None or c.payer == wallet)]


def value_external(components: Iterable[FeeComponent], *, wallet: str, unit: str, prices: Mapping[str, PriceObs],
                   now_mono: float, max_price_age_ms: int | None) -> Valuation:
    """Оценить внешние расходы кандидата в unit по одному свежему источнику на актив. Статья в самом unit — 1:1.
    Без лимита возраста цены проверка не выполняется и это видно в unchecked (решение — у вызывающего)."""
    comps = list(components)
    parts, unknown, unchecked = [], [], []
    sponsored = tuple(c.kind for c in comps if not c.included_in_input_output and not c.refundable
                      and not c.superseded and c.payer is not None and c.payer != wallet)
    with localcontext() as ctx:
        ctx.prec = 50
        for c in own_external(comps, wallet):
            if c.payer is None:
                unknown.append(f"{c.kind}:payer"); continue
            if c.amount_raw is None:
                unknown.append(f"{c.kind}:amount"); continue
            if c.amount_raw == 0:
                continue
            if c.asset == unit:
                parts.append((c.kind, c.human())); continue
            p = prices.get(c.asset)
            if p is None or p.unit != unit:
                unknown.append(f"{c.kind}:price"); continue
            if max_price_age_ms is None:
                unchecked.append(f"{c.kind}:price_age")
            elif (now_mono - p.observed_mono) * 1000 > max_price_age_ms:
                unknown.append(f"{c.kind}:price_stale"); continue
            parts.append((c.kind, c.human() * p.price))
        total = None if unknown else sum((v for _, v in parts), D(0))
    return Valuation(unit=unit, total=total, parts=tuple(parts), unknown=tuple(unknown), sponsored=sponsored,
                     unchecked=tuple(dict.fromkeys(unchecked)))


# --- лампорты: безвозвратное, депозиты, запас ------------------------------------------------------------------
@dataclass(frozen=True)
class NativeSplit:
    """Наши лампорты по статьям. None — в группе есть неизвестная сумма."""
    nonrefundable: int | None       # сеть + tip + невозвратный rent: реализованный расход
    rent_locked: int | None         # депозиты минус возвраты: заблокированный капитал (S18)
    sponsored: int | None           # статьи чужого плательщика (для отчёта)


def _sum(xs: list[int | None]) -> int | None:
    return None if any(x is None for x in xs) else sum(xs)


def native_split(components: Iterable[FeeComponent], wallet: str) -> NativeSplit:
    nonref, dep, ref, spons = [], [], [], []
    for c in components:
        if c.asset != NATIVE_SOL or c.superseded:
            continue
        if c.payer is not None and c.payer != wallet:
            spons.append(c.amount_raw); continue
        if c.kind == "rent_deposit":
            dep.append(c.amount_raw)
        elif c.kind == "rent_refund":
            ref.append(c.amount_raw)
        elif c.kind in ("network_base", "network_priority", "network_total", "tip", "rent_nonrefundable"):
            nonref.append(c.amount_raw if c.payer is not None else None)
    d, r = _sum(dep), _sum(ref)
    return NativeSplit(nonrefundable=_sum(nonref), rent_locked=None if d is None or r is None else d - r,
                       sponsored=_sum(spons))


def native_cash_needed(components: Iterable[FeeComponent], wallet: str, reserve_lamports: int | None) -> int | None:
    """Сколько лампортов должно быть на кошельке ДО отправки: безвозвратное + новые депозиты + запас владельца.
    Возврат депозита приходит после — в покрытие не засчитывается. Нет запаса владельца или неизвестная статья —
    None: проверку остатка (S20) выполнить нельзя, отправка запрещена."""
    if reserve_lamports is None:
        return None
    _raw(reserve_lamports, "reserve")
    comps = list(components)
    s = native_split(comps, wallet)
    deposits = _sum([c.amount_raw for c in comps if c.kind == "rent_deposit" and c.payer in (wallet, None)])
    if s.nonrefundable is None or deposits is None or any(c.payer is None for c in comps if c.asset == NATIVE_SOL):
        return None
    return s.nonrefundable + deposits + reserve_lamports


def with_superseded(components: Iterable[FeeComponent], kinds: frozenset[str]) -> tuple[FeeComponent, ...]:
    """Пометить оценки заданных видов как заменённые (остаются в журнале для сверки, в сумму не идут)."""
    return tuple(replace(c, superseded=True) if c.kind in kinds else c for c in components)


def ceil_div(a: int, b: int) -> int:
    return -(-a // b)


def ceil_decimal(x: D) -> int:
    with localcontext() as c:
        c.prec = 80
        return int(x.to_integral_value(ROUND_CEILING))
