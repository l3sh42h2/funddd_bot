"""Структуры и протоколы ног фазы 2 — ровно по trade_spec §4.

Деньги и количества в СОСТОЯНИИ — только Decimal или сырой int (минимальные единицы токена): float теряет
последний знак шага, а инвариант «0 ≤ токены DEX − |перп| < шаг» проверяется точно (урок Coinglass: единицы
и округления молча ломали учёт). Поля float ниже (impact_pct, gas_usd, tax) — справочные числа для отчёта и
планировщика, в инвариантах и суммах сделки они не участвуют — так их задал §4.
"""
from __future__ import annotations
import hashlib, json
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import ClassVar, Protocol, runtime_checkable

D = Decimal


@dataclass(frozen=True)
class Filters:
    """Фильтры символа перпа (exchangeInfo): шаг цены и количества, минимумы, TIF, которые площадка принимает."""
    tick: D
    step: D
    min_qty: D
    max_qty_limit: D
    max_qty_market: D
    min_notional: D
    tifs: frozenset


@dataclass(frozen=True)
class Book:
    """Стакан: уровни (цена, количество), лучшие первыми; ts — локальное время снимка."""
    bids: tuple[tuple[D, D], ...]
    asks: tuple[tuple[D, D], ...]
    ts: float


@dataclass(frozen=True)
class DexQuote:
    """Котировка агрегатора. amount_* — сырые единицы (10^dec); honeypot/tax — из routerResult (гарды до подписи)."""
    chain: str
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int
    dec_in: int
    dec_out: int
    impact_pct: float
    gas_usd: float
    tax: float
    honeypot: bool
    t: float


@dataclass
class SwapResult:
    """Итог транзакции DEX по чеку. status: ok | reverted | unknown. amount_in — ушло минус возврат роутера
    (роутер возвращает неизрасходованный вход), amount_out — по логам Transfer, а не «баланс после»."""
    tx_hash: str
    status: str
    amount_in: int
    amount_out: int
    gas_wei: int
    gas_usd: float | None
    block: int
    nonce: int


@dataclass
class PerpFill:
    """Итог дочернего IOC. status: FILLED | PARTIALLY_FILLED | EXPIRED | REJECTED | UNKNOWN | NOT_FOUND.
    UNKNOWN — исход не известен (503/-1006/-1007/таймаут): НЕ повторять, выяснять запросом по client_id."""
    client_id: str
    order_id: int | None
    status: str
    qty: D
    avg_px: D
    quote: D
    sign_nonce: int
    err_code: int | None = None


@dataclass
class ClipPlan:
    """Клип плана: вход DEX в сырых единицах и дочерние заявки перпа (qty, px_cap)."""
    seq: int
    dex_in_units: int
    children: list[tuple[D, D]]


@dataclass
class Plan:
    """План сделки для кнопок владельца. missing_owner_keys — чего не хватает в owner.toml для live (в dry план
    строится всё равно и показывает этот список); expires — после него кнопка «да» уже не действует."""
    deal_id: str
    kind: str
    coin: str
    spot: str
    perp: str
    symbol: str
    leg_usd: D
    clips: list[ClipPlan]
    est: dict
    inputs: dict
    missing_owner_keys: list[str]
    expires: float


@runtime_checkable
class SpotLeg(Protocol):
    """Спот-нога на DEX. Неизвестное — None, никогда не 0 (урок: пустое чтение, принятое за «флэт»)."""
    chain: str
    wallet: str

    def quote(self, token_in: str, token_out: str, amount_units: int) -> DexQuote: ...
    def balances(self, token: str) -> dict[str, int | None]: ...        # stable, token, native; None = unknown
    def ensure_allowance(self, token: str, need: int, clip_ref: str) -> SwapResult | None: ...
    def swap(self, token_in: str, token_out: str, amount_units: int, clip_ref: str) -> SwapResult: ...
    def resolve(self, tx_row: dict) -> SwapResult: ...                  # restart reconciliation
    def pool_price(self, token: str) -> D | None: ...                   # slot0 / small re-quote, for recovery r


@runtime_checkable
class PerpLeg(Protocol):
    """Перп-нога. position(): знаковая позиция; None = UNKNOWN, никогда не 0."""
    venue: str

    def filters(self, symbol: str) -> Filters: ...
    def book(self, symbol: str, limit: int = 20) -> Book: ...
    def funding(self, symbol: str) -> tuple[D, D, int]: ...            # mark, last rate, next ts
    def position(self, symbol: str) -> D | None: ...                    # signed; None = UNKNOWN, never 0
    def available_margin(self) -> D | None: ...
    def setup(self, symbol: str, leverage: int, margin_type: str) -> None: ...
    def ioc(self, symbol: str, side: str, qty: D, px_cap: D, client_id: str, reduce_only: bool) -> PerpFill: ...
    def query(self, symbol: str, client_id: str) -> PerpFill: ...
    def fills(self, symbol: str, from_id: int | None) -> list[dict]: ...
    def funding_income(self, symbol: str, start_ms: int) -> list[dict]: ...
    # instrument(symbol) -> PerpInstrument — НЕ в протоколе (ревью 13.09, фаза 1): движок берёт его через getattr;
    # нет метода — вход отказан (m не известен), выход и сверка работают по спецификации сделки


@dataclass(frozen=True)
class PerpInstrument:
    """Контракт перпа по данным площадки. m — токенов базы в одном контракте; None — площадка не дала базы."""
    symbol: str
    base_asset: str | None                # "1000BONK" (baseAsset exchangeInfo)
    base: str | None                      # "BONK" (symbols.norm_symbol_factor)
    m: D | None                           # 1000
    quote_asset: str | None
    contract_type: str | None


def _dstr(x: D | None) -> str | None:
    """Decimal → строка без экспоненты и хвостовых нулей: 1000 / 1E+3 / 1000.0 → «1000» (хеш не зависит от формы)."""
    return None if x is None else format(D(x).normalize(), "f")


@dataclass(frozen=True)
class InstrumentSpec:
    """Что торгует сделка и в каких единицах (ревью 13.09, С1/Н2): спот-токен и контракт перпа, m — токенов в одном
    контракте. Замораживается при входе (deals.inst_json) и в каждом намерении (spec.instrument, spec.inst_hash):
    исполнитель не выбирает инструмент заново по монете. Decimal — строками в JSON; хеш — только по идентичности."""
    chain: str                          # "bsc"
    token: str                          # адрес токена, нижний регистр (гарантирует тот, кто создаёт)
    token_dec: int
    perp_venue: str                     # "aster"
    perp_symbol: str                    # "AIW3USDT", "1000BONKUSDT"
    units_per_contract: D               # m: 1 | 1000 | 1000000
    spot_units_per_token: D = D(1)      # okx·bsc: 1 (токен DEX = единица спота)
    perp_base_asset: str | None = None  # baseAsset exchangeInfo ("1000BONK"); миграция — символ без квоты
    quote_asset: str | None = None
    contract_type: str | None = None
    period_h: D | None = None           # период фандинга на входе (справка, не идентичность)
    ident_ev: str | None = None         # доказательство identity строки таблицы (фаза 2.1 уточнит)
    source: str = ""                    # "exchangeInfo:baseAsset" | "migration:verified_ratio" | "legacy:unverified"
    verified: bool = False              # m подтверждён: биржа + цена (вход) или журнал (миграция)
    verified_ts: float | None = None
    px_ratio: D | None = None           # (цена контракта / m) / цена токена DEX в момент проверки
    why: str | None = None              # почему не подтверждён (для «позиций» и отказов)
    schema: int = 1

    # идентичность — всё, от чего зависят заявки и сверка; справочные поля (baseAsset, период, источник, проверка)
    # в хеш не входят: спецификация из бэкфилла и свежая с биржи для того же контракта дают один хеш
    IDENTITY: ClassVar[tuple[str, ...]] = ("schema", "chain", "token", "token_dec", "spot_units_per_token",
                                           "perp_venue", "perp_symbol", "units_per_contract")
    _DECIMALS: ClassVar[frozenset] = frozenset({"units_per_contract", "spot_units_per_token", "period_h", "px_ratio"})
    SCHEMA: ClassVar[int] = 1

    @property
    def m(self) -> D:
        return self.units_per_contract

    def inst_hash(self) -> str:
        """sha256 канонического JSON полей идентичности, первые 16 hex. Decimal — без экспоненты и хвостовых нулей."""
        ident = {k: (_dstr(getattr(self, k)) if k in self._DECIMALS else getattr(self, k)) for k in self.IDENTITY}
        raw = json.dumps(ident, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        """Все поля: Decimal — строкой, None — как есть, verified — bool, verified_ts — float (jdump безопасен)."""
        out = {}
        for f in fields(self):
            v = getattr(self, f.name)
            out[f.name] = _dstr(v) if (f.name in self._DECIMALS and v is not None) else v
        return out

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> "InstrumentSpec":
        """Обратное to_json. Неизвестная схема, нет обязательного поля, битое число — ValueError (не «угадать»)."""
        try:
            d = json.loads(s)
        except (TypeError, ValueError) as e:
            raise ValueError(f"inst_json не JSON: {e}") from None
        if not isinstance(d, dict):
            raise ValueError("inst_json не объект")
        if d.get("schema") != cls.SCHEMA:
            raise ValueError(f"inst_json: неизвестная схема {d.get('schema')!r}")
        names = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        miss = [k for k in cls.IDENTITY if kw.get(k) is None]
        if miss:
            raise ValueError(f"inst_json: нет полей {miss}")
        try:
            for k in cls._DECIMALS:
                if kw.get(k) is not None:
                    kw[k] = D(str(kw[k]))
                    if not kw[k].is_finite():
                        raise InvalidOperation
            kw["token_dec"] = int(kw["token_dec"])
            kw["verified"] = bool(kw.get("verified", False))
            if kw.get("verified_ts") is not None:
                kw["verified_ts"] = float(kw["verified_ts"])
        except (InvalidOperation, TypeError, ValueError):
            raise ValueError("inst_json: битое число") from None
        if kw["units_per_contract"] <= 0 or kw.get("spot_units_per_token", D(1)) <= 0:
            raise ValueError("inst_json: единицы не положительны")
        return cls(**kw)

    def contracts_for(self, tokens: D, step: D) -> D:
        """Контракты на tokens токенов, вниз к шагу перпа: floor(tokens·s/m / step)·step."""
        return (tokens * self.spot_units_per_token / self.m / step).to_integral_value(ROUND_FLOOR) * step

    def tokens_of(self, contracts: D) -> D:
        return contracts * self.m / self.spot_units_per_token

    def px_per_token(self, px_contract: D) -> D:
        return px_contract * self.spot_units_per_token / self.m
