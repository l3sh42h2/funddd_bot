"""Public report snapshots. No Telegram renderer or trading capability."""
from dataclasses import dataclass
from decimal import Decimal
from typing import Any


def encode(value):
    """Decimal-tagged public values; no dynamic class or object serialization."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('non-finite report amount')
        return {'decimal': str(value)}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if value is None or type(value) in (str, bool, int, float):
        return value
    raise ValueError('unsupported report value')


def decode(value):
    if isinstance(value, dict):
        if set(value) == {'decimal'}:
            result = Decimal(value['decimal'])
            if not result.is_finite():
                raise ValueError('non-finite report amount')
            return result
        return {k: decode(v) for k, v in value.items()}
    if isinstance(value, list):
        return [decode(v) for v in value]
    return value

@dataclass(frozen=True)
class RestartView:
    intent_id: str
    kind: str
    deal_id: str | None = None
    clip: int | None = None
    clips: int | None = None
    matched: bool | None = None         # True — сверено, совпадает; False — расхождение; None — не прочитано
    details: str | None = None
    sim: bool = False
    coin: str | None = None
    hedged: bool | None = None          # ноги ровно (None — не известно)
    delta: Decimal | None = None        # токены − |шорт|
    state: str | None = None            # состояние сделки после сверки
    step: Decimal | None = None
    delta_usd: Decimal | None = None    # голая нога ≈ $ (C: всегда видна)
    m: Decimal | None = None            # токенов в контракте (дельта — токены, шаг — контракты)


@dataclass(frozen=True)
class PositionView:
    deal_id: str
    coin: str
    state: str                          # DealState
    chain: str = "bsc"
    perp_venue: str = "aster"
    spot_qty: Decimal | None = None     # кошелёк
    spot_usd: Decimal | None = None
    perp_qty: Decimal | None = None     # со знаком, − шорт; None = не прочитана
    perp_usd: Decimal | None = None
    upnl_usd: Decimal | None = None
    delta_qty: Decimal | None = None
    funding_usd: Decimal | None = None
    funding_n: int | None = None
    held_s: float | None = None
    liq_dist_pct: Decimal | None = None
    liq_alert_pct: Decimal | None = None    # порог тревоги, замороженный на входе (число или ½ расстояния входа)
    reason: str | None = None           # причина паузы
    sim: bool = False
    step: Decimal | None = None
    leg_usd: Decimal | None = None      # размер сделки на ногу
    payback_h: Decimal | None = None    # до окупаемости (report.payback_h); 0 — окупилась
    delta_usd: Decimal | None = None    # голая нога ≈ $ (дельта × цена спота)
    pnl_now_usd: Decimal | None = None  # оценка сделки (trade/marks.py): PnL по рынку сейчас …
    pnl_exit_usd: Decimal | None = None     # … и если закрыть всё прямо сейчас (котировка OKX, стакан, комиссии)
    pnl_uncovered: bool = False         # стакан мельче шорта — выход оценён по худшему уровню
    m_unknown: bool = False             # множитель контракта не известен (ревью 13.09, M3): только «выход» целиком


@dataclass(frozen=True)
class StatusView:
    ts: float
    mode: str                           # dry | readonly | live
    running: str | None = None          # id идущего намерения
    paused: bool = False
    open_deals: int | None = None
    max_open_deals: int | None = None
    tg_last_ok_ago_s: float | None = None
    tg_reconnects_24h: int = 0
    sender_fails: int = 0
    data_ages: tuple[tuple[str, float | None], ...] = ()     # («таблица», 8), («котировка OKX», 40)
    aster_weight_pct: Decimal | None = None
    chain: str = "bsc"
    wallet_stable: Decimal | None = None
    wallet_native: Decimal | None = None
    margin_avail: Decimal | None = None
    cap_usd: Decimal | None = None
    daily_stop: Any = None
    daily_used_usd: Decimal | None = None
    checks: tuple[tuple[str, bool | None, str], ...] = ()    # (что, ок?, подробность) — readonly-проверки
    missing_owner_keys: tuple[str, ...] = ()
