"""Структуры и протоколы ног фазы 2 — ровно по trade_spec §4.

Деньги и количества в СОСТОЯНИИ — только Decimal или сырой int (минимальные единицы токена): float теряет
последний знак шага, а инвариант «0 ≤ токены DEX − |перп| < шаг» проверяется точно (урок Coinglass: единицы
и округления молча ломали учёт). Поля float ниже (impact_pct, gas_usd, tax) — справочные числа для отчёта и
планировщика, в инвариантах и суммах сделки они не участвуют — так их задал §4.
"""
from __future__ import annotations
import hashlib, json, re
from dataclasses import dataclass, fields
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import ClassVar, Protocol, runtime_checkable
from .solana import TOKEN_PROGRAMS as _SOL_TOKEN_PROGRAMS
from .solana.b58 import is_pubkey as _is_pubkey

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


# schema 2 (связка SOL×HL, ТЗ 13.09 §2.1): статусы identity — как в реестре instruments.json
IDENTITY_STATUSES = ("candidate", "pending_underlying_evidence", "verified_source", "reviewed_override", "revoked")
_V2_CHAINS = frozenset({"solana"})     # schema 2 пока только для Solana; BSC/Aster живут на schema 1 байт-в-байт
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def _sol_key(v, what: str) -> None:
    """base58 ровно 32 байта, регистр как есть: другой регистр — другой mint (S01, S02). Значение в текст не пишем."""
    if not isinstance(v, str) or not _is_pubkey(v):
        raise ValueError(f"inst_json: {what} — нужен base58 32 байта (регистр как есть)")


@dataclass(frozen=True)
class InstrumentSpec:
    """Что торгует сделка и в каких единицах (ревью 13.09, С1/Н2): спот-токен и контракт перпа, m — токенов в одном
    контракте. Замораживается при входе (deals.inst_json) и в каждом намерении (spec.instrument, spec.inst_hash):
    исполнитель не выбирает инструмент заново по монете. Decimal — строками в JSON; хеш — только по идентичности.

    schema 1 — связка okx·bsc × Aster (фаза 1): JSON, хеш и читатель прежние байт-в-байт (DQA9Q не перепривязывается).
    schema 2 — профиль, сеть и genesis, mint base58 с регистром, программа токена, котировка спота, перп
    (площадка/сеть/dex/fullcoin), scope счёта, Fs/Fp, статус identity (ТЗ SOL×HL §2.1). В терминах ТЗ:
    Fs = spot_units_per_token, Fp = units_per_contract (при Fs = 1 это прежний m)."""
    chain: str                          # "bsc" | "solana" (tconfig.canonical_chain)
    token: str                          # schema 1: адрес EVM нижним регистром; schema 2: mint base58 как есть
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
    # --- schema 2 (SOL×HL). У schema 1 все None и в JSON не пишутся ---------------------------------------------
    profile_id: str | None = None           # "sol_best_hyperliquid" (schema 1 — неявно bsc_okx_aster)
    instrument_id: str | None = None        # запись реестра runtime/instruments.json
    instrument_version: int | None = None
    network: str | None = None              # свой id сети: "solana-mainnet" (OKX 501 — id провайдера, не сети)
    genesis_hash: str | None = None         # getGenesisHash сети спота
    token_program: str | None = None        # программа mint (Token / Token-2022): от неё ATA и разбор чека
    token_extensions: tuple[str, ...] | None = None     # расширения mint (jsonParsed), проверенные при записи
    quote_mint: str | None = None           # котировка спота (USDC Solana), base58 как есть
    quote_dec: int | None = None
    quote_program: str | None = None
    perp_network: str | None = None         # "mainnet"
    perp_dex: str | None = None             # dex HIP-3 ("para"); "" — основной dex
    perp_account: str | None = None         # scope счёта: чья net-позиция (сеть/счёт/dex)
    perp_collateral: str | None = None      # обеспечение перпа ("token:0"); None — не наблюдалось
    perp_asset_id: int | None = None        # наблюдавшийся asset id: перед планом сверка с метой, не идентичность
    identity_status: str | None = None      # статус соответствия mint ↔ перп на момент записи (не идентичность)
    identity_hash: str | None = None        # identity_hash записи реестра
    identity_expires_at: str | None = None  # срок identity (ISO 8601): вход после срока — отказ, выход — нет

    # идентичность — всё, от чего зависят заявки и сверка; справочные поля (baseAsset, период, источник, проверка)
    # в хеш не входят: спецификация из бэкфилла и свежая с биржи для того же контракта дают один хеш
    IDENTITY: ClassVar[tuple[str, ...]] = ("schema", "chain", "token", "token_dec", "spot_units_per_token",
                                           "perp_venue", "perp_symbol", "units_per_contract")
    # schema 2: смена любого из этих полей = другой инструмент (ТЗ §2.1). Статус и срок identity, asset id, источник —
    # справка: продление решения владельца или новый срез меты не перепривязывают сделку и не отменяют план
    IDENTITY_V2: ClassVar[tuple[str, ...]] = (
        "schema", "profile_id", "instrument_id", "chain", "network", "genesis_hash", "token", "token_dec",
        "token_program", "token_extensions", "quote_mint", "quote_dec", "quote_program", "spot_units_per_token",
        "perp_venue", "perp_network", "perp_dex", "perp_symbol", "perp_account", "perp_collateral",
        "units_per_contract")
    # поля schema 1 — ровно те, что были в фазе 1: её JSON и читатель не меняются ни на байт
    FIELDS_V1: ClassVar[tuple[str, ...]] = (
        "chain", "token", "token_dec", "perp_venue", "perp_symbol", "units_per_contract", "spot_units_per_token",
        "perp_base_asset", "quote_asset", "contract_type", "period_h", "ident_ev", "source", "verified", "verified_ts",
        "px_ratio", "why", "schema")
    _DECIMALS: ClassVar[frozenset] = frozenset({"units_per_contract", "spot_units_per_token", "period_h", "px_ratio"})
    _INTS_V2: ClassVar[tuple[str, ...]] = ("quote_dec", "instrument_version", "perp_asset_id")
    _OPTIONAL_V2: ClassVar[frozenset] = frozenset({"perp_collateral"})    # в хеше, но может быть не наблюдено
    SCHEMA: ClassVar[int] = 1
    SCHEMAS: ClassVar[tuple[int, ...]] = (1, 2)

    def __post_init__(self):
        if self.schema == 2:
            self._check_v2()

    def _check_v2(self) -> None:
        """schema 2 проверяется при создании (не при чтении только): битую спецификацию нельзя даже построить."""
        miss = [k for k in self.IDENTITY_V2 if getattr(self, k) is None and k not in self._OPTIONAL_V2]
        if miss or self.identity_status is None:
            raise ValueError(f"inst_json: нет полей {miss or ['identity_status']}")
        if self.chain not in _V2_CHAINS:
            raise ValueError(f"inst_json: schema 2 для сети {self.chain!r} не поддержана")
        _sol_key(self.token, "mint")
        _sol_key(self.quote_mint, "mint котировки")
        _sol_key(self.genesis_hash, "genesis_hash")
        if self.token == self.quote_mint:
            raise ValueError("inst_json: спот и котировка — один mint")
        for k in ("token_program", "quote_program"):
            if getattr(self, k) not in _SOL_TOKEN_PROGRAMS:
                raise ValueError(f"inst_json: {k} — не программа токенов Solana")
        for k in ("token_dec", "quote_dec"):
            v = getattr(self, k)
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
                raise ValueError(f"inst_json: {k} = {v!r}")
        if not isinstance(self.token_extensions, tuple) or not all(isinstance(x, str) and x
                                                                   for x in self.token_extensions):
            raise ValueError("inst_json: token_extensions — список имён")
        for k in ("profile_id", "instrument_id", "network", "perp_venue", "perp_network", "perp_account"):
            v = getattr(self, k)
            if not isinstance(v, str) or not _NAME_RE.match(v):
                raise ValueError(f"inst_json: {k} — неверный формат")
        dex, sym = self.perp_dex, self.perp_symbol
        if not isinstance(dex, str) or not isinstance(sym, str) or (
                dex and (not sym.startswith(dex + ":") or len(sym) == len(dex) + 1)) or (not dex and ":" in sym):
            raise ValueError("inst_json: perp_symbol — нужен полный fullcoin «dex:МОНЕТА» (регистр монеты как у биржи)")
        if self.identity_status not in IDENTITY_STATUSES:
            raise ValueError(f"inst_json: identity_status {self.identity_status!r}")
        if self.units_per_contract <= 0 or self.spot_units_per_token <= 0:
            raise ValueError("inst_json: единицы не положительны")

    @property
    def m(self) -> D:
        return self.units_per_contract

    @property
    def fs(self) -> D:
        """Fs ТЗ: единиц базового актива в одном спот-токене."""
        return self.spot_units_per_token

    @property
    def fp(self) -> D:
        """Fp ТЗ: единиц базового актива в одной единице размера перпа."""
        return self.units_per_contract

    @property
    def perp_scope(self) -> str | None:
        """Кто владеет net-позицией (ТЗ §6): площадка|сеть|счёт|dex|fullcoin. Одна незакрытая сделка на scope —
        уникальный индекс trade.db. schema 1 — None (её держит прежний индекс deals(perp_venue, symbol))."""
        if self.schema < 2:
            return None
        return "|".join((self.perp_venue, self.perp_network, self.perp_account, self.perp_dex, self.perp_symbol))

    def _ident_value(self, k: str):
        v = getattr(self, k)
        if k in self._DECIMALS:
            return _dstr(v)
        if isinstance(v, tuple):
            return sorted(v)
        return v

    def inst_hash(self) -> str:
        """sha256 канонического JSON полей идентичности, первые 16 hex. Decimal — без экспоненты и хвостовых нулей."""
        if self.schema == 1:
            ident = {k: (_dstr(getattr(self, k)) if k in self._DECIMALS else getattr(self, k)) for k in self.IDENTITY}
        else:
            ident = {k: self._ident_value(k) for k in self.IDENTITY_V2}
        raw = json.dumps(ident, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def as_dict(self) -> dict:
        """Все поля: Decimal — строкой, None — как есть, verified — bool, verified_ts — float (jdump безопасен).
        schema 1 — только поля фазы 1 (тот же JSON)."""
        names = self.FIELDS_V1 if self.schema == 1 else tuple(f.name for f in fields(self))
        out = {}
        for n in names:
            v = getattr(self, n)
            if n in self._DECIMALS and v is not None:
                v = _dstr(v)
            elif isinstance(v, tuple):
                v = list(v)
            out[n] = v
        return out

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, s: str) -> "InstrumentSpec":
        """Обратное to_json. Неизвестная схема, нет обязательного поля, битое число — ValueError (не «угадать»).
        schema 1 читается ровно как в фазе 1: поля schema 2 в её JSON не принимаются во внимание."""
        try:
            d = json.loads(s)
        except (TypeError, ValueError) as e:
            raise ValueError(f"inst_json не JSON: {e}") from None
        if not isinstance(d, dict):
            raise ValueError("inst_json не объект")
        schema = d.get("schema")
        if isinstance(schema, bool) or schema not in cls.SCHEMAS:
            raise ValueError(f"inst_json: неизвестная схема {schema!r}")
        names = set(cls.FIELDS_V1) if schema == 1 else {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in names}
        ident = cls.IDENTITY if schema == 1 else tuple(k for k in cls.IDENTITY_V2 if k not in cls._OPTIONAL_V2)
        miss = [k for k in ident if kw.get(k) is None]
        if miss:
            raise ValueError(f"inst_json: нет полей {miss}")
        try:
            for k in cls._DECIMALS:
                if kw.get(k) is not None:
                    if schema == 2 and isinstance(kw[k], (bool, float)):     # float JSON теряет запись числа
                        raise InvalidOperation
                    kw[k] = D(str(kw[k]))
                    if not kw[k].is_finite():
                        raise InvalidOperation
            kw["token_dec"] = int(kw["token_dec"])
            kw["verified"] = bool(kw.get("verified", False))
            if kw.get("verified_ts") is not None:
                kw["verified_ts"] = float(kw["verified_ts"])
            if schema == 2:
                for k in cls._INTS_V2:
                    v = kw.get(k)
                    if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                        raise ValueError
                if not isinstance(kw["token_extensions"], list):
                    raise ValueError
                kw["token_extensions"] = tuple(kw["token_extensions"])
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
