"""Реестр исполняемых инструментов связки «спот Solana × перп Hyperliquid» (ТЗ SOL×HL 13.09, §2.1, §4).

Что здесь:
- base58 (адреса Solana, секреты, подписи) — строго: алфавит, длина в байтах. Регистр значим: адрес Solana НЕ
  приводится к нижнему или верхнему регистру никогда (другой регистр = другие байты = другой mint);
- сети: собственный id сети (solana-mainnet) отдельно от id провайдера (OKX chainIndex 501) и genesis hash;
- InstrumentSpec — неизменяемая запись: спот (mint, программа токена, decimals, расширения), котировка (USDC), единицы
  Fs/Fp, перп (площадка, dex, fullcoin, account scope), identity (статус и основание), срез метаданных; хеши;
- загрузка runtime/instruments.json — строго, как owner.toml: неизвестный ключ или повтор ключа — ошибка;
- единицы: raw ↔ human без float, target_short = floor_step(T·Fs/Fp, h), Δ = T·Fs − S·Fp.

Чего здесь нет: чтения RPC/HL (trade/solana.py, trade/hyperliquid_trade.py) и решения о соответствии mint ↔ перп:
его принимает владелец (reviewed_override) или даёт прямой источник (verified_source). Реестр — не разрешение торговли:
live только при положительном статусе, неистёкшем сроке, принятых единицах и enabled_for_live.
Сделка хранит свою копию записи (to_dict) и её identity_hash: правка реестра не перепривязывает открытую сделку.
"""
from __future__ import annotations
import hashlib, json, re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Context, Decimal, Inexact, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping
from .. import config
from .solana import MAINNET_GENESIS, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT
from .solana import b58 as _b58
from .types import InstrumentSpec as DealInstrument

# --- base58 ------------------------------------------------------------------------------------
# Кодек один на проект — trade/solana/b58.py (поток Solana). Здесь — строгая обёртка: пусто и слишком длинно — ошибка
# (у кодека пустая строка — ноль байт); ошибки — ValueError, строку в текст не пишут (она может быть секретом).
B58_ALPHABET = _b58.ALPHABET
_B58_MAX_CHARS = 256                    # подписи и секреты ≤ 88; длиннее — не наши данные
b58encode = _b58.b58encode


def b58decode(s: str) -> bytes:
    if not isinstance(s, str) or not s or len(s) > _B58_MAX_CHARS:
        raise ValueError("base58: пусто или слишком длинно")
    return _b58.b58decode(s)


def b58_len(s: Any) -> int | None:
    """Длина в байтах или None, если это не base58."""
    try:
        return len(b58decode(s))
    except ValueError:
        return None


def is_sol_address(s: Any) -> bool:
    """Открытый ключ Solana: base58 ровно 32 байта, строка как есть (без пробелов по краям)."""
    return isinstance(s, str) and s == s.strip() and b58_len(s) == 32


def sol_address(s: Any, what: str = "адрес Solana") -> str:
    if not is_sol_address(s):
        raise ValueError(f"{what}: нужен base58 32 байта, регистр как есть")
    return s


# --- сети -----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Network:
    id: str                 # собственный id: solana-mainnet
    family: str             # solana | evm
    chain: str              # имя сети в таблице/коллекторе (tconfig.canonical_chain): solana
    genesis_hash: str | None
    native_symbol: str
    native_decimals: int


SOLANA_MAINNET = "solana-mainnet"
# getGenesisHash mainnet-beta, сверено 13.09 12:37Z по двум публичным RPC (sol_plan/probe_rpc_20260913.json)
SOLANA_MAINNET_GENESIS = MAINNET_GENESIS
NETWORKS: Mapping[str, Network] = {
    SOLANA_MAINNET: Network(SOLANA_MAINNET, "solana", "solana", SOLANA_MAINNET_GENESIS, "SOL", 9),
}
# синоним нормализует только ИМЯ сети; адресов и mint не касается. «501» — id сети у OKX, не наш id: не синоним
_NETWORK_ALIASES = {"sol": SOLANA_MAINNET, "solana": SOLANA_MAINNET, SOLANA_MAINNET: SOLANA_MAINNET}
_EVM_CHAINS = frozenset({"bsc", "robinhood"})
_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def canonical_network(name: Any) -> str:
    n = str(name).strip().lower()
    if n in _NETWORK_ALIASES:
        return _NETWORK_ALIASES[n]
    raise KeyError(f"неизвестная сеть: {name}")


def canonical_asset_address(chain: str, addr: str) -> str:
    """Ключ адреса для сравнения: EVM — нижний регистр (байт-в-байт как раньше), Solana — исходный base58 после
    проверки 32 байт. Сеть неизвестна — KeyError (не угадывать)."""
    c = str(chain).strip().lower()
    if c in _NETWORK_ALIASES:
        return sol_address(addr)
    from . import tconfig
    c = tconfig.canonical_chain(c)
    if c == "solana":
        return sol_address(addr)
    if c in _EVM_CHAINS:
        if not isinstance(addr, str) or not _EVM_ADDR_RE.match(addr.strip()):
            raise ValueError("адрес EVM: нужно 0x + 40 hex")
        return addr.strip().lower()
    raise KeyError(f"неизвестная сеть: {chain}")


# --- токены Solana ----------------------------------------------------------------------------------
# TOKEN_PROGRAM (SPL Token) и TOKEN_2022_PROGRAM (у ANSEM) — из trade/solana
TOKEN_PROGRAMS = {TOKEN_PROGRAM: "spl-token", TOKEN_2022_PROGRAM: "spl-token-2022"}
# Первый выпуск (ТЗ §2.2): у mint допустимы только расширения метаданных; у token account — immutableOwner.
# Имена — как в jsonParsed RPC. TransferFee/TransferHook/PermanentDelegate/DefaultAccountState/NonTransferable/
# confidential/interest/scaled и неизвестные — отказ до отдельной реализации (неизвестное ≠ «налог 0»).
MINT_EXTENSIONS_V1 = ("metadataPointer", "tokenMetadata")
ACCOUNT_EXTENSIONS_V1 = ("immutableOwner",)
USDC_SOLANA = USDC_MINT                                            # нативный USDC (не мостовой)
# котировочные активы профилей: (сеть, mint) → (символ, decimals, программа токена)
QUOTE_ASSETS: Mapping[tuple[str, str], tuple[str, int, str]] = {
    (SOLANA_MAINNET, USDC_SOLANA): ("USDC", 6, TOKEN_PROGRAM),
}

# --- профили и статусы ----------------------------------------------------------------------------
SOL_HL = "sol_best_hyperliquid"
SOL_GATE = "sol_best_gate"
SOL_ASTER = "sol_best_aster"
# профиль → (сеть спота, площадка перпа, сети перпа)
PROFILE_ROUTES: Mapping[str, tuple[str, str, tuple[str, ...]]] = {
    SOL_HL: (SOLANA_MAINNET, "hyperliquid", ("mainnet",)),
    # CEX deployments use a real environment label when the native metadata
    # has one; it is not a fabricated blockchain network.
    SOL_GATE: (SOLANA_MAINNET, "gate", ("mainnet",)),
    SOL_ASTER: (SOLANA_MAINNET, "aster", ("mainnet",)),
}
IDENTITY_STATUSES = ("candidate", "pending_underlying_evidence", "verified_source", "reviewed_override", "revoked")
LIVE_IDENTITY = ("verified_source", "reviewed_override")
UNITS_STATUSES = ("candidate_pending_mapping_acceptance", "accepted", "revoked")
ASSET_ID_POLICIES = ("resolve_and_verify_from_current_full_metadata",)
REGISTRY_FILE = "instruments.json"
REGISTRY_SCHEMA = 1

_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")
_DEX_RE = re.compile(r"^[a-z0-9]{1,16}$")
_EXT_RE = re.compile(r"^[a-z][A-Za-z0-9]{0,63}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:._-]{1,200}$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.json$")


class RegistryError(ValueError):
    """instruments.json не читается или запись не того вида. Реестр отклоняется целиком: торговля по битому
    реестру — это торговля не тем токеном."""


class AmbiguousInstrument(RegistryError):
    """Под текст команды подходят несколько записей — нужен точный instrument_id (ТЗ V01)."""

    def __init__(self, text: str, ids: tuple[str, ...]):
        self.ids = ids
        super().__init__(f"«{text}» неоднозначно: {', '.join(ids)} — укажите instrument_id")


# --- записи ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SpotAsset:
    network: str
    genesis_hash: str | None
    mint: str                       # base58 как есть
    decimals: int
    token_program: str
    extensions: tuple[str, ...]     # наблюдавшиеся расширения mint (jsonParsed)


@dataclass(frozen=True)
class QuoteAsset:
    network: str
    mint: str
    decimals: int
    token_program: str
    symbol: str


@dataclass(frozen=True)
class Units:
    fs: Decimal                     # единиц базового актива в одном спот-токене
    fp: Decimal                     # единиц базового актива в одной единице размера перпа
    status: str


@dataclass(frozen=True)
class PerpMarket:
    network: str
    venue: str
    dex: str
    fullcoin: str                   # точное имя HIP-3: para:ANSEM (регистр монеты значим)
    account_id: str | None          # scope счёта: сеть/мастер/субаккаунт/dex/обеспечение
    collateral_token_id: int | None
    asset_id: int | None            # наблюдавшийся; перед планом — сверка с текущей метой (policy)
    asset_id_policy: str
    sz_decimals: int | None
    max_leverage: int | None
    margin_mode: str | None

    @property
    def coin(self) -> str:
        return self.fullcoin.split(":", 1)[1] if ":" in self.fullcoin else self.fullcoin.rsplit("_", 1)[0]


@dataclass(frozen=True)
class Identity:
    status: str
    evidence: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    source_scanner_status: str | None
    source_scanner_reason: str | None
    direct_oracle_mint_evidence: str | None
    reviewed_by: str | None
    reviewed_at: str | None
    review_reason: str | None
    expires_at: str | None
    observed_at: str | None


@dataclass(frozen=True)
class Snapshot:
    solana_observed_at: str | None
    solana_slot: int | None
    hyperliquid_observed_at: str | None
    must_refresh_before_live: bool


def _dstr(d: Decimal) -> str:
    """Каноническая строка Decimal: 1.0 и 1 — одно и то же число и одна строка в хеше."""
    s = format(d.normalize(), "f")
    return s if s != "-0" else "0"


def _canon(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: str
    version: int
    profile_id: str
    display_symbol: str
    enabled_for_live: bool
    spot: SpotAsset
    quote: QuoteAsset
    units: Units
    perp: PerpMarket
    identity: Identity
    snapshot: Snapshot

    @property
    def asset_key(self) -> tuple[str, str]:
        return (self.spot.network, self.spot.mint)

    @property
    def perp_key(self) -> tuple[str, str, str, str]:
        return (self.perp.network, self.perp.venue, self.perp.dex, self.perp.fullcoin)

    def identity_fields(self) -> dict:
        """Поля, смена которых = другой инструмент (ТЗ §2.1): только новой версией, открытые сделки не перепривязываются."""
        s, q, u, p = self.spot, self.quote, self.units, self.perp
        return {"instrument_id": self.instrument_id, "version": self.version, "profile_id": self.profile_id,
                "spot": {"network": s.network, "genesis_hash": s.genesis_hash, "mint": s.mint, "decimals": s.decimals,
                         "token_program": s.token_program, "extensions": sorted(s.extensions)},
                "quote": {"network": q.network, "mint": q.mint, "decimals": q.decimals, "token_program": q.token_program},
                "units": {"fs": _dstr(u.fs), "fp": _dstr(u.fp)},
                "perp": {"network": p.network, "venue": p.venue, "dex": p.dex, "fullcoin": p.fullcoin,
                         "account_id": p.account_id}}

    @property
    def identity_hash(self) -> str:
        return "sha256:" + hashlib.sha256(_canon(self.identity_fields())).hexdigest()

    @property
    def record_hash(self) -> str:
        """Хеш всей записи (с identity-статусом и срезом) — для журнала: какой именно записью шёл план."""
        return "sha256:" + hashlib.sha256(_canon(self.to_dict())).hexdigest()

    def to_dict(self) -> dict:
        """Форма записи instruments.json (parse_instrument(to_dict()) == self)."""
        s, q, u, p, i, n = self.spot, self.quote, self.units, self.perp, self.identity, self.snapshot
        ident = {"status": i.status, "source_scanner_status": i.source_scanner_status,
                 "source_scanner_reason": i.source_scanner_reason, "evidence": list(i.evidence),
                 "direct_oracle_mint_evidence": i.direct_oracle_mint_evidence, "reviewed_by": i.reviewed_by,
                 "reviewed_at": i.reviewed_at, "review_reason": i.review_reason, "expires_at": i.expires_at}
        if i.evidence_hashes:
            ident["evidence_hashes"] = list(i.evidence_hashes)
        if i.observed_at is not None:
            ident["observed_at"] = i.observed_at
        return {
            "instrument_id": self.instrument_id, "version": self.version, "profile_id": self.profile_id,
            "display_symbol": self.display_symbol, "enabled_for_live": self.enabled_for_live,
            "spot": {"network": s.network, "genesis_hash": s.genesis_hash, "mint": s.mint, "decimals": s.decimals,
                     "token_program": s.token_program, "observed_extensions": list(s.extensions)},
            "quote": {"network": q.network, "mint": q.mint, "decimals": q.decimals, "token_program": q.token_program},
            "units": {"spot_units_in_base": _dstr(u.fs), "perp_units_in_base": _dstr(u.fp), "status": u.status},
            "perp": {"network": p.network, "venue": p.venue, "dex": p.dex, "fullcoin": p.fullcoin,
                     "account_id": p.account_id, "collateral_token_id_observed": p.collateral_token_id,
                     "observed_asset_id": p.asset_id, "asset_id_policy": p.asset_id_policy,
                     "observed_sz_decimals": p.sz_decimals, "observed_max_leverage": p.max_leverage,
                     "observed_margin_mode": p.margin_mode},
            "identity": ident,
            "snapshot": {"solana_observed_at": n.solana_observed_at, "solana_slot": n.solana_slot,
                         "hyperliquid_observed_at": n.hyperliquid_observed_at,
                         "must_refresh_before_live": n.must_refresh_before_live},
        }

    def to_json(self) -> str:
        return _canon(self.to_dict()).decode("utf-8")


# --- разбор записи ---------------------------------------------------------------------------------
def _obj(v: Any, where: str, req: tuple[str, ...], opt: tuple[str, ...] = ()) -> dict:
    if not isinstance(v, dict):
        raise RegistryError(f"{where}: нужен объект")
    unknown = sorted(set(v) - set(req) - set(opt))
    if unknown:
        raise RegistryError(f"{where}: неизвестный ключ {', '.join(unknown)}")
    miss = [k for k in req if k not in v]
    if miss:
        raise RegistryError(f"{where}: нет ключа {', '.join(miss)}")
    return v


def _int(v: Any, where: str, lo: int | None = None, hi: int | None = None, null: bool = False) -> int | None:
    if v is None and null:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        raise RegistryError(f"{where} = {v!r}: нужно целое")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise RegistryError(f"{where} = {v}: вне диапазона [{lo}, {hi}]")
    return v


def _str(v: Any, where: str, rx: re.Pattern | None = None, null: bool = False) -> str | None:
    if v is None and null:
        return None
    if not isinstance(v, str) or not v or v != v.strip() or (rx is not None and not rx.match(v)):
        raise RegistryError(f"{where} = {v!r}: неверный формат")
    return v


def _enum(v: Any, where: str, words: tuple[str, ...]) -> str:
    if not isinstance(v, str) or v not in words:
        raise RegistryError(f"{where} = {v!r}: допустимо {' | '.join(words)}")
    return v


def _bool(v: Any, where: str) -> bool:
    if not isinstance(v, bool):
        raise RegistryError(f"{where} = {v!r}: нужно true или false")
    return v


def _factor(v: Any, where: str) -> Decimal:
    """Fs/Fp — строка десятичного числа или целое (float JSON в реестр не пускаем: теряет запись)."""
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise RegistryError(f"{where} = {v!r}: нужна строка числа, например \"1\"")
    try:
        d = Decimal(v.strip() if isinstance(v, str) else v)
    except InvalidOperation:
        raise RegistryError(f"{where} = {v!r}: не число") from None
    if not d.is_finite() or d <= 0:
        raise RegistryError(f"{where} = {v!r}: нужно конечное число > 0")
    return d


def _ts(v: Any, where: str, null: bool = True) -> str | None:
    """Время ISO 8601 с часовым поясом; хранится строкой как в файле."""
    if v is None and null:
        return None
    if not isinstance(v, str):
        raise RegistryError(f"{where} = {v!r}: нужно время ISO 8601")
    try:
        t = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise RegistryError(f"{where} = {v!r}: нужно время ISO 8601") from None
    if t.tzinfo is None:
        raise RegistryError(f"{where} = {v!r}: нужно время с часовым поясом (UTC)")
    return v


def ts_epoch(v: str) -> float:
    return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()


def _sol(v: Any, where: str, null: bool = False) -> str | None:
    if v is None and null:
        return None
    if not is_sol_address(v):
        raise RegistryError(f"{where}: нужен base58 32 байта (регистр как есть)")
    return v


def _str_list(v: Any, where: str, rx: re.Pattern | None = None) -> tuple[str, ...]:
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise RegistryError(f"{where}: нужен список строк")
    if rx is not None:
        for x in v:
            if not rx.match(x):
                raise RegistryError(f"{where}: {x!r} — неверный формат")
    if len(set(v)) != len(v):
        raise RegistryError(f"{where}: повтор в списке")
    return tuple(v)


def parse_instrument(d: Any, where: str = "instrument") -> InstrumentSpec:
    d = _obj(d, where, ("instrument_id", "version", "profile_id", "display_symbol", "enabled_for_live", "spot", "quote",
                        "units", "perp", "identity", "snapshot"))
    iid = _str(d["instrument_id"], f"{where}.instrument_id", _ID_RE)
    where = f"instruments[{iid}]"
    version = _int(d["version"], f"{where}.version", lo=1)
    profile = _enum(d["profile_id"], f"{where}.profile_id", tuple(PROFILE_ROUTES))
    net_spot, venue_want, perp_nets = PROFILE_ROUTES[profile]

    s = _obj(d["spot"], f"{where}.spot", ("network", "genesis_hash", "mint", "decimals", "token_program",
                                          "observed_extensions"))
    snet = _enum(s["network"], f"{where}.spot.network", (net_spot,))
    genesis = _sol(s["genesis_hash"], f"{where}.spot.genesis_hash", null=True)
    if genesis is not None and genesis != NETWORKS[snet].genesis_hash:
        raise RegistryError(f"{where}.spot.genesis_hash = {genesis}: не genesis сети {snet}")
    spot = SpotAsset(snet, genesis, _sol(s["mint"], f"{where}.spot.mint"),
                     _int(s["decimals"], f"{where}.spot.decimals", 0, 255),
                     _enum(s["token_program"], f"{where}.spot.token_program", tuple(TOKEN_PROGRAMS)),
                     _str_list(s["observed_extensions"], f"{where}.spot.observed_extensions", _EXT_RE))
    if spot.token_program == TOKEN_PROGRAM and spot.extensions:
        raise RegistryError(f"{where}.spot: у классического SPL Token расширений не бывает")

    q = _obj(d["quote"], f"{where}.quote", ("network", "mint", "decimals", "token_program"))
    qnet = _enum(q["network"], f"{where}.quote.network", (snet,))
    qmint = _sol(q["mint"], f"{where}.quote.mint")
    known = QUOTE_ASSETS.get((qnet, qmint))
    if known is None:
        raise RegistryError(f"{where}.quote.mint = {qmint}: не котировочный актив профиля (USDC {USDC_SOLANA})")
    qdec = _int(q["decimals"], f"{where}.quote.decimals", 0, 255)
    qprog = _enum(q["token_program"], f"{where}.quote.token_program", tuple(TOKEN_PROGRAMS))
    if (qdec, qprog) != known[1:]:
        raise RegistryError(f"{where}.quote: decimals/программа {qdec}/{qprog} ≠ {known[0]} {known[1]}/{known[2]}")
    if qmint == spot.mint:
        raise RegistryError(f"{where}: спот и котировка — один mint")
    quote = QuoteAsset(qnet, qmint, qdec, qprog, known[0])

    u = _obj(d["units"], f"{where}.units", ("spot_units_in_base", "perp_units_in_base", "status"))
    units = Units(_factor(u["spot_units_in_base"], f"{where}.units.spot_units_in_base"),
                  _factor(u["perp_units_in_base"], f"{where}.units.perp_units_in_base"),
                  _enum(u["status"], f"{where}.units.status", UNITS_STATUSES))

    p = _obj(d["perp"], f"{where}.perp", ("network", "venue", "dex", "fullcoin", "account_id"),
             ("collateral_token_id_observed", "observed_asset_id", "asset_id_policy", "observed_sz_decimals",
              "observed_max_leverage", "observed_margin_mode"))
    pnet = _enum(p["network"], f"{where}.perp.network", perp_nets)
    venue = _enum(p["venue"], f"{where}.perp.venue", (venue_want,))
    if venue == "hyperliquid":
        dex = _str(p["dex"], f"{where}.perp.dex", _DEX_RE)
        full = _str(p["fullcoin"], f"{where}.perp.fullcoin")
        pre, _, coin = full.partition(":")
        if pre != dex or not _SYMBOL_RE.match(coin):
            raise RegistryError(f"{where}.perp.fullcoin = {full!r}: нужно «{dex}:МОНЕТА"
                                "(регистр монеты как у биржи)")
    else:
        if p["dex"] not in (None, ""):
            raise RegistryError(f"{where}.perp.dex: у {venue} HIP-3 dex не задаётся")
        dex = ""
        full = _str(p["fullcoin"], f"{where}.perp.fullcoin", re.compile(r"^[A-Za-z0-9_]{2,64}$"))
    perp = PerpMarket(pnet, venue, dex, full, _str(p["account_id"], f"{where}.perp.account_id", _ACCOUNT_RE, null=True),
                      _int(p.get("collateral_token_id_observed"), f"{where}.perp.collateral_token_id_observed", 0,
                           null=True),
                      _int(p.get("observed_asset_id"), f"{where}.perp.observed_asset_id", 0, null=True),
                      _enum(p.get("asset_id_policy", ASSET_ID_POLICIES[0]), f"{where}.perp.asset_id_policy",
                            ASSET_ID_POLICIES),
                      _int(p.get("observed_sz_decimals"), f"{where}.perp.observed_sz_decimals", 0, 18, null=True),
                      _int(p.get("observed_max_leverage"), f"{where}.perp.observed_max_leverage", 1, null=True),
                      _str(p.get("observed_margin_mode"), f"{where}.perp.observed_margin_mode", null=True))

    i = _obj(d["identity"], f"{where}.identity", ("status", "evidence"),
             ("source_scanner_status", "source_scanner_reason", "direct_oracle_mint_evidence", "reviewed_by",
              "reviewed_at", "review_reason", "expires_at", "evidence_hashes", "observed_at"))
    ev = _str_list(i["evidence"], f"{where}.identity.evidence")
    eh = _str_list(i.get("evidence_hashes") or [], f"{where}.identity.evidence_hashes", _HASH_RE)
    if eh and len(eh) != len(ev):
        raise RegistryError(f"{where}.identity.evidence_hashes: {len(eh)} хешей на {len(ev)} источников")
    ident = Identity(_enum(i["status"], f"{where}.identity.status", IDENTITY_STATUSES), ev, eh,
                     _str(i.get("source_scanner_status"), f"{where}.identity.source_scanner_status", null=True),
                     _str(i.get("source_scanner_reason"), f"{where}.identity.source_scanner_reason", null=True),
                     _str(i.get("direct_oracle_mint_evidence"), f"{where}.identity.direct_oracle_mint_evidence",
                          null=True),
                     _str(i.get("reviewed_by"), f"{where}.identity.reviewed_by", null=True),
                     _ts(i.get("reviewed_at"), f"{where}.identity.reviewed_at"),
                     _str(i.get("review_reason"), f"{where}.identity.review_reason", null=True),
                     _ts(i.get("expires_at"), f"{where}.identity.expires_at"),
                     _ts(i.get("observed_at"), f"{where}.identity.observed_at"))
    # решение владельца хранит кто, когда, почему, до какого срока и на чём основано — разработчик его не придумывает
    if ident.status == "reviewed_override":
        miss = [k for k in ("reviewed_by", "reviewed_at", "review_reason", "expires_at") if getattr(ident, k) is None]
        if miss or not ident.evidence:
            raise RegistryError(f"{where}.identity: reviewed_override без {', '.join(miss or ['evidence'])}")
    if ident.status == "verified_source" and (ident.direct_oracle_mint_evidence is None or ident.expires_at is None):
        raise RegistryError(f"{where}.identity: verified_source без direct_oracle_mint_evidence и expires_at")

    n = _obj(d["snapshot"], f"{where}.snapshot", ("must_refresh_before_live",),
             ("solana_observed_at", "solana_slot", "hyperliquid_observed_at"))
    snap = Snapshot(_ts(n.get("solana_observed_at"), f"{where}.snapshot.solana_observed_at"),
                    _int(n.get("solana_slot"), f"{where}.snapshot.solana_slot", 0, null=True),
                    _ts(n.get("hyperliquid_observed_at"), f"{where}.snapshot.hyperliquid_observed_at"),
                    _bool(n["must_refresh_before_live"], f"{where}.snapshot.must_refresh_before_live"))
    return InstrumentSpec(iid, version, profile, _str(d["display_symbol"], f"{where}.display_symbol", _SYMBOL_RE),
                          _bool(d["enabled_for_live"], f"{where}.enabled_for_live"), spot, quote, units, perp, ident,
                          snap)


def spec_from_json(s: str) -> InstrumentSpec:
    """Копия записи из сделки (to_json) обратно — те же проверки, что у файла."""
    return parse_instrument(_loads(s, "копия инструмента"))


def check_successor(old: InstrumentSpec, new: InstrumentSpec) -> None:
    """Правка записи: поля идентичности меняются только новой версией; версия назад не идёт."""
    if old.instrument_id != new.instrument_id:
        raise RegistryError(f"{new.instrument_id} ≠ {old.instrument_id}: другой инструмент")
    if new.version < old.version:
        raise RegistryError(f"{new.instrument_id}: версия {new.version} меньше {old.version}")
    if new.version == old.version and new.identity_hash != old.identity_hash:
        raise RegistryError(f"{new.instrument_id} v{new.version}: изменились mint/сеть/программа/decimals/единицы/"
                            "перп/счёт без новой версии")


# --- допуск ----------------------------------------------------------------------------------------
def _now_epoch(now: float | datetime | None) -> float:
    if now is None:
        return datetime.now(timezone.utc).timestamp()
    return now.timestamp() if isinstance(now, datetime) else float(now)


def entry_blockers(spec: InstrumentSpec, now: float | datetime | None = None,
                   allowed_mint_extensions: tuple[str, ...] | frozenset = MINT_EXTENSIONS_V1) -> list[str]:
    """Причины, по которым НОВЫЙ вход/добор по записи запрещён (пусто — запись допускает live; остальные ворота —
    свежий mint, мета HL, счёт, лимиты — проверяются отдельно перед каждым планом)."""
    out = []
    t = _now_epoch(now)
    if not spec.enabled_for_live:
        out.append("запись выключена для live (enabled_for_live = false)")
    i = spec.identity
    if i.status not in LIVE_IDENTITY:
        out.append(f"identity: {i.status} — для live нужен verified_source или reviewed_override")
    if i.expires_at is None:
        out.append("identity: нет срока expires_at")
    elif ts_epoch(i.expires_at) <= t:
        out.append(f"identity: срок истёк {i.expires_at}")
    if spec.units.status != "accepted":
        out.append(f"единицы: {spec.units.status} — соответствие Fs/Fp не принято")
    if spec.spot.genesis_hash is None:
        out.append("spot: не задан genesis_hash сети")
    if spec.perp.account_id is None:
        out.append("perp: не задан account_id (scope счёта)")
    allowed = set(allowed_mint_extensions) & set(MINT_EXTENSIONS_V1)
    bad = [e for e in spec.spot.extensions if e not in allowed]
    if bad:
        out.append(f"spot: расширения mint вне допуска: {', '.join(bad)}")
    return out


def protective_blockers(spec: InstrumentSpec) -> list[str]:
    """Для защитного действия по УЖЕ открытой позиции (ТЗ §2.1, G05/G06): истёкший срок записи и выключенный live не
    мешают устранить известную дельту; отозванное соответствие или единицы — мешают (нужна отдельная политика)."""
    out = []
    if spec.identity.status == "revoked":
        out.append("identity отозвана: соответствие mint ↔ перп оспорено — только отдельная corrective/unwind политика")
    if spec.units.status == "revoked":
        out.append("единицы отозваны: Fs/Fp оспорены — только отдельная corrective/unwind политика")
    return out


def mint_mismatches(spec: InstrumentSpec, value: Mapping | None) -> list[str]:
    """Свежий getAccountInfo(mint, encoding=jsonParsed).result.value против записи (ТЗ §2.2, S03–S06). Пусто —
    совпадает. Разбор сырых TLV и проверка token account — trade/solana.py."""
    if value is None:
        return ["mint: аккаунт не найден"]
    out = []
    if value.get("owner") != spec.spot.token_program:
        out.append(f"mint: программа {value.get('owner')} ≠ {spec.spot.token_program}")
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "mint" or not isinstance(parsed.get("info"), dict):
        return out + ["mint: нет разобранных данных mint (jsonParsed)"]
    info = parsed["info"]
    if info.get("isInitialized") is not True:
        out.append("mint: не инициализирован")
    if info.get("decimals") != spec.spot.decimals:
        out.append(f"mint: decimals {info.get('decimals')} ≠ {spec.spot.decimals}")
    raw = info.get("extensions") or []
    names = [e.get("extension") if isinstance(e, dict) else None for e in raw]
    if any(not isinstance(nm, str) for nm in names):
        out.append("mint: расширение без имени — формат не распознан")
    names = [nm for nm in names if isinstance(nm, str)]
    unsupported = [nm for nm in names if nm not in MINT_EXTENSIONS_V1]
    if unsupported:
        out.append(f"mint: неподдержанные расширения {', '.join(unsupported)} (первый выпуск: только метаданные)")
    added = sorted(set(names) - set(spec.spot.extensions))
    gone = sorted(set(spec.spot.extensions) - set(names))
    if added or gone:
        out.append(f"mint: расширения изменились (+{','.join(added) or '—'} −{','.join(gone) or '—'}) — нужна новая "
                   "версия записи")
    return out


# --- реестр ----------------------------------------------------------------------------------------
def _pairs_no_dups(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise RegistryError(f"повтор ключа {k!r}")
        d[k] = v
    return d


def _no_const(c):
    raise RegistryError(f"недопустимое число {c}")


def _loads(text: str, where: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs_no_dups, parse_float=Decimal, parse_constant=_no_const)
    except RegistryError:
        raise
    except ValueError as e:
        raise RegistryError(f"{where}: не JSON — {e}") from None


@dataclass(frozen=True)
class Registry:
    specs: tuple[InstrumentSpec, ...]
    path: str
    sha256: str | None                  # None — файла нет: реестр пуст, торговать нечем

    def versions(self, instrument_id: str) -> tuple[InstrumentSpec, ...]:
        return tuple(sorted((s for s in self.specs if s.instrument_id == instrument_id), key=lambda s: s.version))

    def get(self, instrument_id: str, version: int | None = None) -> InstrumentSpec:
        vs = self.versions(instrument_id)
        if version is not None:
            vs = tuple(s for s in vs if s.version == version)
        if not vs:
            raise RegistryError(f"нет записи {instrument_id}" + (f" v{version}" if version is not None else ""))
        return vs[-1]

    def latest(self) -> tuple[InstrumentSpec, ...]:
        ids = sorted({s.instrument_id for s in self.specs})
        return tuple(self.get(i) for i in ids)

    def by_asset(self, network: str, mint: str) -> tuple[InstrumentSpec, ...]:
        """Точный ключ (сеть, mint) — mint сравнивается как есть: другой регистр — не тот актив (S02)."""
        net = canonical_network(network)
        return tuple(s for s in self.latest() if s.asset_key == (net, mint))

    def by_perp(self, network: str, venue: str, dex: str, fullcoin: str) -> tuple[InstrumentSpec, ...]:
        return tuple(s for s in self.latest() if s.perp_key == (network, venue, dex, fullcoin))

    def resolve(self, profile_id: str, text: str, allowed: tuple[str, ...] | None = None,
                perp_dex: str | None = None) -> InstrumentSpec:
        """Текст команды («ANSEM», «para:ANSEM», instrument_id, mint) → единственная запись профиля. Совпадение — точное
        по регистру; отозванные не участвуют; несколько — AmbiguousInstrument; ни одной — подсказка без автовыбора."""
        pool = [s for s in self.latest() if s.profile_id == profile_id and s.identity.status != "revoked"
                and (allowed is None or s.instrument_id in allowed) and (perp_dex is None or s.perp.dex == perp_dex)]

        def names(s: InstrumentSpec) -> tuple[str, ...]:
            return (s.instrument_id, s.display_symbol, s.perp.fullcoin, s.perp.coin, s.spot.mint)

        hit = [s for s in pool if text in names(s)]
        if len(hit) == 1:
            return hit[0]
        if len(hit) > 1:
            raise AmbiguousInstrument(text, tuple(s.instrument_id for s in hit))
        near = sorted({s.instrument_id for s in pool if text.lower() in (n.lower() for n in names(s))})
        hint = f" (есть с другим регистром: {', '.join(near)})" if near else ""
        raise RegistryError(f"«{text}»: нет записи профиля {profile_id}{hint}")


def registry_path(name: str | None = None) -> Path:
    n = name or REGISTRY_FILE
    if not _FILE_RE.match(n):
        raise RegistryError(f"имя реестра {n!r}: только файл *.json в runtime/, без каталогов")
    return config.RUNTIME / n


def parse_registry(text: str, path: str = "instruments.json") -> Registry:
    doc = _obj(_loads(text, path), path, ("schema_version", "instruments"))
    if _int(doc["schema_version"], f"{path}.schema_version") != REGISTRY_SCHEMA:
        raise RegistryError(f"{path}: schema_version {doc['schema_version']} (знаю {REGISTRY_SCHEMA})")
    if not isinstance(doc["instruments"], list):
        raise RegistryError(f"{path}.instruments: нужен список")
    specs = tuple(parse_instrument(x, f"{path}.instruments[{n}]") for n, x in enumerate(doc["instruments"]))
    seen: dict[tuple[str, int], InstrumentSpec] = {}
    for s in specs:
        if (s.instrument_id, s.version) in seen:
            raise RegistryError(f"{path}: повтор {s.instrument_id} v{s.version}")
        seen[(s.instrument_id, s.version)] = s
    return Registry(specs, path, hashlib.sha256(text.encode("utf-8")).hexdigest())


def load_registry(path: Path | str | None = None) -> Registry:
    """Свежее чтение (как owner.toml — без кэша). Нет файла — пустой реестр: торговать нечем, dry/readonly живут."""
    p = Path(path) if path else registry_path()
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return Registry((), str(p), None)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RegistryError(f"{p}: не UTF-8") from None
    return parse_registry(text, str(p))


# --- единицы (ТЗ §4) ------------------------------------------------------------------------------
_EXACT = Context(prec=100, traps=[Inexact, InvalidOperation])


def raw_to_human(raw: int, decimals: int) -> Decimal:
    """Сырые единицы → Decimal без округления: 1234567 при 6 → 1.234567."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"сырое количество — целое ≥ 0, получено {raw!r}")
    if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 255:
        raise ValueError(f"decimals {decimals!r}")
    return Decimal(f"{raw}E-{decimals}")


def human_to_raw(x: Decimal | int, decimals: int) -> int:
    """Decimal → сырые единицы только если число точно ложится на шаг 10^-decimals (иначе ValueError, не округление)."""
    if isinstance(x, bool) or not isinstance(x, (Decimal, int)):
        raise ValueError(f"нужно Decimal/int, получено {type(x).__name__}")
    d = Decimal(x)
    if not d.is_finite() or d < 0:
        raise ValueError(f"количество {x}: нужно конечное ≥ 0")
    try:
        with localcontext(_EXACT):
            r = d.scaleb(decimals)
            n = r.to_integral_value()
            if n != r:
                raise Inexact
    except (Inexact, InvalidOperation):
        raise ValueError(f"{x} не ложится на {decimals} знаков") from None
    return int(n)


def floor_step(x: Decimal, h: Decimal) -> Decimal:
    """floor(x/h)·h — точно: деление в контексте с округлением вниз не может перепрыгнуть целое."""
    if not isinstance(h, Decimal) or not h.is_finite() or h <= 0:
        raise ValueError(f"шаг {h!r}: нужно Decimal > 0")
    with localcontext(Context(prec=100, rounding=ROUND_FLOOR)):
        return (x / h).to_integral_value(rounding=ROUND_FLOOR) * h


def target_short(spot_raw: int, spec: InstrumentSpec, step: Decimal) -> Decimal:
    """target_short(T) = floor_step(T·Fs/Fp, h) в единицах размера перпа (S — положительное число)."""
    t = raw_to_human(spot_raw, spec.spot.decimals)
    with localcontext(Context(prec=100, rounding=ROUND_FLOOR)):
        return floor_step(t * spec.units.fs / spec.units.fp, step)


def delta_base(spot_raw: int, short: Decimal, spec: InstrumentSpec) -> Decimal:
    """Δ = T·Fs − S·Fp в единицах базового актива (не токенах и не контрактах, если Fs/Fp ≠ 1)."""
    if not isinstance(short, Decimal) or short < 0:
        raise ValueError("шорт — Decimal ≥ 0 (raw szi у HL отрицателен; перевод в S — у адаптера)")
    t = raw_to_human(spot_raw, spec.spot.decimals)
    with localcontext(_EXACT):
        return t * spec.units.fs - short * spec.units.fp


# --- замороженная спецификация сделки (types.InstrumentSpec schema 2) --------------------------------------------
def _canonical_hl_account_id(value: str | None) -> str | None:
    """Normalize only EVM addresses in runtime's HL scope; preserve all other identities.

    Registry records/hashes and already stored deal specifications stay unchanged.
    Legacy/opaque account identifiers retain their exact comparison semantics.
    """
    if value is None:
        return None
    parts = value.split(":")
    if (len(parts) == 5 and parts[0] == "hyperliquid"
            and _EVM_ADDR_RE.fullmatch(parts[2]) and _EVM_ADDR_RE.fullmatch(parts[3])):
        parts[2], parts[3] = parts[2].lower(), parts[3].lower()
        return ":".join(parts)
    return value


def deal_spec(spec: InstrumentSpec, *, account_id: str | None = None,
              now: float | datetime | None = None) -> DealInstrument:
    """Запись реестра → спецификация сделки schema 2 (deals.inst_json, inst_hash намерений): mint с регистром,
    программа токена, котировка, Fs/Fp, перп (площадка/сеть/dex/fullcoin) и scope счёта, статус identity.
    Счёт — из записи или из owner.toml (wallets.sol_hl); заданы оба и разные — отказ, не угадываем. Допуск live здесь
    не решается (entry_blockers): спецификация описывает, ЧТО торгует сделка, а не разрешение торговать."""
    acct = _canonical_hl_account_id(spec.perp.account_id)
    account_id = _canonical_hl_account_id(account_id)
    if account_id is not None:
        if acct is not None and acct != account_id:
            raise RegistryError(f"{spec.instrument_id}: счёт {account_id} ≠ account_id записи {acct}")
        acct = account_id
    if acct is None:
        raise RegistryError(f"{spec.instrument_id}: не задан account_id (scope счёта перпа)")
    if spec.spot.genesis_hash is None:
        raise RegistryError(f"{spec.instrument_id}: не задан genesis_hash сети спота")
    ok = spec.units.status == "accepted"
    col = spec.perp.collateral_token_id
    try:
        return DealInstrument(
            chain=NETWORKS[spec.spot.network].chain, token=spec.spot.mint, token_dec=spec.spot.decimals,
            perp_venue=spec.perp.venue, perp_symbol=spec.perp.fullcoin, units_per_contract=spec.units.fp,
            spot_units_per_token=spec.units.fs, perp_base_asset=spec.perp.coin, quote_asset=spec.quote.symbol,
            source=f"registry:{spec.instrument_id}@v{spec.version}", verified=ok, verified_ts=_now_epoch(now),
            why=None if ok else f"единицы: {spec.units.status}", schema=2, profile_id=spec.profile_id,
            instrument_id=spec.instrument_id, instrument_version=spec.version, network=spec.spot.network,
            genesis_hash=spec.spot.genesis_hash, token_program=spec.spot.token_program,
            token_extensions=spec.spot.extensions, quote_mint=spec.quote.mint, quote_dec=spec.quote.decimals,
            quote_program=spec.quote.token_program, perp_network=spec.perp.network, perp_dex=spec.perp.dex,
            perp_account=acct, perp_collateral=None if col is None else f"token:{col}", perp_asset_id=spec.perp.asset_id,
            identity_status=spec.identity.status, identity_hash=spec.identity_hash,
            identity_expires_at=spec.identity.expires_at)
    except ValueError as e:
        raise RegistryError(f"{spec.instrument_id}: {e}") from None
