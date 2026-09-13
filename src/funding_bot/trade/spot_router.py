"""Выбор маршрута спота Solana (ТЗ §5; SOLANA_ROUTERS §3–4; CODE_INTEGRATION §5): общий тип кандидата и роутер.

Роутер ничего не подписывает и не отправляет. Он собирает кандидатов трёх путей (jupiter_order_v2, jupiter_build_v2,
okx_solana_v6) с единым сроком сбора, проверяет их одними правилами и выбирает одного победителя клипа:
- сравниваются только ответы на один и тот же запрос (сеть, mint, программы, decimals, сторона, ExactIn raw, кошелёк,
  slippage — request_hash, R01);
- вход: минимум (списание USDC + НАШИ внешние расходы) / ожидаемые токены; выход: максимум USDC − внешние расходы.
  Комиссия, уже внутри потоков (included_in_input_output), второй раз не вычитается (R02–R04);
- неизвестная обязательная статья, нет min-out, чужой запрос, протухшая котировка — кандидат исключён с причиной,
  а не «цена 0» (R05, R07, R08). Все кандидаты и причины исключения пишутся в журнал (route_candidates);
- экономика пары (глубина HL на весь размер, а не mid/лучший бид; маржа шорта с плечом и резервом владельца;
  возраст стакана — часами момента оценки) — обязательный фильтр, а не способ объявить дорогой своп «самым
  дешёвым» (§5.1 п.5–6);
- инструкции провайдера — против манифеста v1 уже на уровне JSON (json_manifest); tip — только получателям из
  allowlist владельца (R14);
- ворота групп: вход — нужны проверенные Jupiter (Order или Build) И OKX, иначе «неполное сравнение» (§5.2, G07);
  выход — достаточно одной группы, если владелец включил такую политику;
- одна повторная сессия сбора, затем план истёк (§5.1 п.7); при неразрешённом UNKNOWN новый маршрут не выбирается (R12).

Лимиты владельца (RouteLimits) денежных значений по умолчанию не имеют: нет значения — причина
«limit_missing:<имя>», кандидат не проходит в live, но виден в превью-ранжировании (M1 readonly).

Разбор и подпись самих Solana-транзакций здесь не делаются: сборка v0-message с ALT, симуляция и полная проверка
по манифесту — за протоколами TxAssembler / Simulator / PayloadValidator (ждут solders и поток Solana-ядра).
"""
from __future__ import annotations
import base64, binascii, hashlib, heapq, itertools, json, logging, threading, time
from concurrent.futures import ThreadPoolExecutor, wait as _fwait
from contextlib import contextmanager
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from typing import Callable, Mapping, Protocol, Sequence, runtime_checkable
from .fees import FeeComponent, PriceObs, Valuation, dec_str, human, lamports, native_split, value_external, NATIVE_SOL
from .solana import b58 as _b58
from .types import Book

log = logging.getLogger(__name__)
D = Decimal

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
COMPUTE_BUDGET_PROGRAM = "ComputeBudget111111111111111111111111111111"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAMS = frozenset({TOKEN_PROGRAM, TOKEN_2022_PROGRAM})

PATHS = ("jupiter_order_v2", "jupiter_build_v2", "okx_solana_v6")
GROUP_OF = {"jupiter_order_v2": "jupiter", "jupiter_build_v2": "jupiter", "okx_solana_v6": "okx"}
GROUPS = ("jupiter", "okx")
PURPOSES = ("recovery", "exit", "entry", "mark", "scanner")      # порядок = приоритет квоты (§5.2)
METRIC_VERSION = "sol_route_v1"
U64_MAX = 2 ** 64 - 1

# Причины, которые не портят данные кандидата, а только не пускают его в live: превью их показывает
LIVE_ONLY = frozenset({"limit_missing", "not_validated", "not_simulated", "capability", "block_height_unknown",
                       "hedge_unchecked", "recipient_unverified", "source_unverified", "rent_unknown",
                       "blockhash_unknown", "price_impact_unknown", "perp_fee_unknown", "margin_unknown",
                       "margin_insufficient"})
# Причины, при которых имеет смысл одна повторная сессия сбора (book_stale — только если стакан берётся заново)
TRANSIENT = frozenset({"stale", "deadline", "rate_limited", "blockhash_expiring", "rfq_expired", "book_stale",
                       "http", "error", "simulation_error", "hedge_fetch_error"})


def code_of(reason: str) -> str:
    return reason.split(":", 1)[0]


def live_only(reason: str) -> bool:
    return code_of(reason) in LIVE_ONLY


class SchemaError(ValueError):
    """Ответ провайдера не по контракту — кандидат непригоден (не «исправляем» и не угадываем)."""


class PayloadError(ValueError):
    """Payload не декодируется объявленной кодировкой (G13: кодировка — явное поле, не эвристика)."""


# --- base58 (адреса Solana регистрозависимы: никакого lower) -------------------------------------------------------
# Кодек один на проект — trade/solana/b58.py; здесь — обёртка с ошибкой контракта маршрута (PayloadError)
B58_ALPHABET = _b58.ALPHABET
b58encode = _b58.b58encode


def b58decode(s: str) -> bytes:
    if not isinstance(s, str) or not s or s != s.strip():
        raise PayloadError("base58: пустая строка или пробелы по краям")
    try:
        return _b58.b58decode(s)
    except _b58.Base58Error as e:
        raise PayloadError(str(e)) from None


def is_pubkey(s) -> bool:
    """Строка base58 ровно из 32 байт. Регистр значим: вариант с другим регистром — другой (или битый) адрес."""
    try:
        return isinstance(s, str) and len(b58decode(s)) == 32
    except PayloadError:
        return False


def decode_bytes(data: str, encoding: str) -> bytes:
    """Байты payload строго объявленной кодировкой: Jupiter transaction — base64, OKX /swap tx.data — base58."""
    if not isinstance(data, str) or not data:
        raise PayloadError("пустой payload")
    if encoding == "base64":
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError) as e:
            raise PayloadError(f"base64: {e}") from None
    elif encoding == "base58":
        raw = b58decode(data)
    else:
        raise PayloadError(f"кодировка {encoding!r} не поддержана")
    if not raw:
        raise PayloadError("пустые байты")
    return raw


def u64(x, what: str) -> int:
    """Целое без знака из ответа API (строка цифр или int), в пределах u64. float/bool/знак — ошибка контракта."""
    if isinstance(x, bool):
        raise SchemaError(f"{what}: bool вместо числа")
    if isinstance(x, int):
        v = x
    elif isinstance(x, str) and x.isascii() and x.isdigit():
        v = int(x)
    else:
        raise SchemaError(f"{what}: не целое без знака ({str(x)[:40]!r})")
    if not 0 <= v <= U64_MAX:
        raise SchemaError(f"{what}: вне u64")
    return v


def body_hash(body) -> str:
    """Хеш исходного ответа (канонический JSON) — в журнал кандидата вместо самого ответа."""
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                                     default=str).encode()).hexdigest()


# --- активы и запрос ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AssetRef:
    """Актив из замороженной спецификации сделки: mint и программа токена — из RPC, не из тикера/метаданных API."""
    mint: str
    program: str
    decimals: int
    symbol: str = ""            # только подпись

    def __post_init__(self):
        if not is_pubkey(self.mint):
            raise ValueError(f"mint не base58-адрес: {self.mint!r}")
        if self.program not in TOKEN_PROGRAMS:
            raise ValueError(f"программа токена не поддержана: {self.program!r}")
        if isinstance(self.decimals, bool) or not isinstance(self.decimals, int) or not 0 <= self.decimals <= 18:
            raise ValueError(f"decimals: {self.decimals!r}")


@dataclass(frozen=True)
class QuoteRequest:
    """Один клип: ExactIn raw входного актива. Вход — фиксированный USDC, выход — фиксированные токены сделки
    (§5.1: не сравнивать разные объёмы, maxIn=баланс не использовать)."""
    side: str                          # entry | exit
    input: AssetRef
    output: AssetRef
    amount_in_raw: int
    wallet: str
    slippage_bps: int
    genesis_hash: str
    deadline_mono: float               # общий срок сбора (монотонные часы)
    purpose: str = "entry"             # приоритет квоты: recovery → exit → entry → mark → scanner
    chain: str = "solana"
    input_account: str | None = None   # ожидаемые токен-счета кошелька (ATA по программе mint — от Solana-ядра)
    output_account: str | None = None
    account_rent: tuple[tuple[str, int | None], ...] = ()   # mint → rent создания НАШЕГО счёта: 0 есть, None неизв.

    def __post_init__(self):
        if self.side not in ("entry", "exit"):
            raise ValueError(f"side: {self.side!r}")
        if self.purpose not in PURPOSES:
            raise ValueError(f"purpose: {self.purpose!r}")
        if self.input.mint == self.output.mint:
            raise ValueError("вход и выход — один mint")
        if isinstance(self.amount_in_raw, bool) or not isinstance(self.amount_in_raw, int) or not 0 < self.amount_in_raw <= U64_MAX:
            raise ValueError(f"amount_in_raw: целое > 0, а не {self.amount_in_raw!r}")
        if isinstance(self.slippage_bps, bool) or not isinstance(self.slippage_bps, int) or not 0 < self.slippage_bps < 10000:
            raise ValueError(f"slippage_bps: {self.slippage_bps!r}")
        for name in ("wallet", "genesis_hash"):
            if not is_pubkey(getattr(self, name)):
                raise ValueError(f"{name}: не base58 из 32 байт")
        for name in ("input_account", "output_account"):
            v = getattr(self, name)
            if v is not None and not is_pubkey(v):
                raise ValueError(f"{name}: не base58-адрес")

    @property
    def request_hash(self) -> str:
        parts = (self.chain, self.genesis_hash, self.side, self.input.mint, self.input.program, str(self.input.decimals),
                 self.output.mint, self.output.program, str(self.output.decimals), str(self.amount_in_raw),
                 self.wallet, str(self.slippage_bps))
        return hashlib.sha256("|".join(parts).encode()).hexdigest()

    @property
    def unit_mint(self) -> str:
        """Единица учёта клипа — стейбл-сторона: вход при покупке, выход при продаже."""
        return self.input.mint if self.side == "entry" else self.output.mint

    def rent_for(self, mint: str) -> int | None:
        for m, v in self.account_rent:
            if m == mint:
                return v
        return None

    def asset(self, mint: str) -> AssetRef | None:
        return self.input if mint == self.input.mint else self.output if mint == self.output.mint else None


# --- инструкции (JSON провайдера → типы; данные инструкций у Jupiter и OKX — base64) -----------------------------
@dataclass(frozen=True)
class AccountMeta:
    pubkey: str
    is_signer: bool
    is_writable: bool


@dataclass(frozen=True)
class Ix:
    program_id: str
    accounts: tuple[AccountMeta, ...]
    data: bytes

    def acc(self, i: int) -> AccountMeta | None:
        return self.accounts[i] if 0 <= i < len(self.accounts) else None


def ix_from_json(o, where: str) -> Ix:
    if not isinstance(o, dict):
        raise SchemaError(f"{where}: инструкция не объект")
    pid = o.get("programId")
    if not is_pubkey(pid):
        raise SchemaError(f"{where}: programId не base58-адрес")
    accs = o.get("accounts")
    if not isinstance(accs, list):
        raise SchemaError(f"{where}: accounts не список")
    metas = []
    for i, a in enumerate(accs):
        if (not isinstance(a, dict) or not is_pubkey(a.get("pubkey")) or not isinstance(a.get("isSigner"), bool)
                or not isinstance(a.get("isWritable"), bool)):
            raise SchemaError(f"{where}: accounts[{i}] не по контракту")
        metas.append(AccountMeta(a["pubkey"], a["isSigner"], a["isWritable"]))
    data = o.get("data")
    if not isinstance(data, str):
        raise SchemaError(f"{where}: data не строка")
    try:
        raw = base64.b64decode(data, validate=True) if data else b""
    except (binascii.Error, ValueError):
        raise SchemaError(f"{where}: data не base64") from None
    return Ix(pid, tuple(metas), raw)


def compute_budget(ixs: Sequence[Ix]) -> tuple[int | None, int | None, list[str]]:
    """(лимит CU, цена CU в микролампортах, проблемы). Каждая инструкция ComputeBudget — не более одного раза."""
    limit = price = None
    seen: set[int] = set()
    problems: list[str] = []
    for ix in ixs:
        if ix.program_id != COMPUTE_BUDGET_PROGRAM:
            continue
        tag = ix.data[0] if ix.data else -1
        if tag in seen:
            problems.append("cu_conflict")
        seen.add(tag)
        if ix.accounts:
            problems.append("cb_accounts")
        if tag == 2 and len(ix.data) == 5:
            limit = int.from_bytes(ix.data[1:5], "little")
        elif tag == 3 and len(ix.data) == 9:
            price = int.from_bytes(ix.data[1:9], "little")
        elif tag == 4 and len(ix.data) == 5:
            pass                                   # SetLoadedAccountsDataSizeLimit — на деньги не влияет
        else:
            problems.append(f"cb_unknown:{tag}")
    return limit, price, problems


def ata_creates(ixs: Sequence[Ix]) -> tuple[list[tuple[str, str, str, str, str]], list[str]]:
    """Create/CreateIdempotent ATA: [(payer, ata, owner, mint, token_program)], проблемы."""
    out, problems = [], []
    for ix in ixs:
        if ix.program_id != ATA_PROGRAM:
            continue
        if ix.data not in (b"", b"\x00", b"\x01") or len(ix.accounts) < 6:
            problems.append("ata_other")
            continue
        a = ix.accounts
        out.append((a[0].pubkey, a[1].pubkey, a[2].pubkey, a[3].pubkey, a[5].pubkey))
    return out, problems


def system_ixs(ixs: Sequence[Ix]) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Переводы System Transfer [(from, to, lamports)] и прочие инструкции System (проблемы)."""
    out, problems = [], []
    for ix in ixs:
        if ix.program_id != SYSTEM_PROGRAM:
            continue
        tag = int.from_bytes(ix.data[:4], "little") if len(ix.data) >= 4 else -1
        if tag == 2 and len(ix.data) == 12 and len(ix.accounts) >= 2:
            out.append((ix.accounts[0].pubkey, ix.accounts[1].pubkey, int.from_bytes(ix.data[4:12], "little")))
        else:
            problems.append(f"system_ix:{tag}")
    return out, problems


def signers_of(ixs: Sequence[Ix], wallet: str) -> tuple[str, ...]:
    """Подписанты по метам инструкций; плательщик комиссии (наш кошелёк) — всегда первый."""
    return tuple(dict.fromkeys([wallet] + [m.pubkey for ix in ixs for m in ix.accounts if m.is_signer]))


def preparation_fees(ixs: Sequence[Ix], req: "QuoteRequest", source: str,
                     tip_ix: Ix | None = None) -> tuple[list[FeeComponent], list[str]]:
    """Подготовка в инструкциях провайдера: создание ATA (депозит rent — по данным Solana-ядра, неизвестно = None),
    tip и прочие переводы SOL. Адрес ATA сверяется с ожидаемым счётом запроса; вывод PDA — Solana-ядро (S07)."""
    fees: list[FeeComponent] = []
    r: list[str] = []
    creates, p = ata_creates(ixs)
    r += p
    for payer, ata, owner, mint, prog in creates:
        a = req.asset(mint)
        if owner != req.wallet:
            r.append("ata_owner")
        if a is None:
            r.append("ata_foreign_mint")
        elif prog != a.program:
            r.append("ata_token_program")
        want = req.input_account if mint == req.input.mint else req.output_account if mint == req.output.mint else None
        if want is not None and ata != want:
            r.append("ata_address")
        fees.append(lamports("rent_deposit", req.rent_for(mint), payer, estimated=True, source=source,
                             note=f"создание счёта {mint[:6]}…"))
    tip_tr, tip_p = system_ixs([tip_ix] if tip_ix is not None else [])
    if tip_ix is not None and (tip_p or len(tip_tr) != 1):
        fees.append(lamports("tip", None, req.wallet, estimated=True, source=source, note="tip не разобран"))
        r.append("tip_unparsed")
    fees += [lamports("tip", lam, frm, estimated=False, source=source, note=f"tip → {to[:6]}…", recipient=to)
             for frm, to, lam in tip_tr]
    tr, p2 = system_ixs([ix for ix in ixs if ix is not tip_ix])
    r += p2
    if tr:            # USDC↔токен переводов SOL не требует: любой такой перевод — отказ до решения валидатора
        r.append("system_transfer_unexpected")
        fees += [lamports("tip", lam, frm, estimated=False, source=source, note=f"перевод SOL → {to[:6]}… вне tip",
                          recipient=to) for frm, to, lam in tr]
    return fees, r


# --- манифест v1 на уровне JSON-инструкций (S10, S12) --------------------------------------------------------------
# Что вообще может стоять рядом со свопом USDC↔токен. Проверяется по JSON провайдера ДО сборки и без solders:
# программа + дискриминатор каждой инструкции всех списков. Полный message после ALT, вложенные CPI и эффекты
# симуляции — валидатор (ждёт solders); этот барьер его не заменяет, а не пускает очевидное раньше.
MANIFEST_VERSION = "sol_json_manifest_v1"
PROGRAM_NAMES = {TOKEN_PROGRAM: "Token", TOKEN_2022_PROGRAM: "Token2022", ATA_PROGRAM: "ATA",
                 COMPUTE_BUDGET_PROGRAM: "ComputeBudget", SYSTEM_PROGRAM: "System"}
CB_ALLOWED = frozenset({2, 3, 4})          # SetComputeUnitLimit, SetComputeUnitPrice, SetLoadedAccountsDataSizeLimit
ATA_ALLOWED = frozenset({b"", b"\x00", b"\x01"})   # Create, CreateIdempotent (RecoverNested и прочее — нет)


def ix_disc(ix: Ix) -> str:
    """Дискриминатор для причины: тег u32 у System, первый байт у нативных программ, 8 байт у Anchor/чужих."""
    if not ix.data:
        return "empty"
    if ix.program_id == SYSTEM_PROGRAM:
        return str(int.from_bytes(ix.data[:4], "little")) if len(ix.data) >= 4 else "short"
    if ix.program_id in PROGRAM_NAMES:
        return str(ix.data[0])
    return ix.data[:8].hex()


def json_manifest(ixs: Sequence[Ix], req: "QuoteRequest", *, router_program: str, router_ix: Ix | None = None,
                  tip_ix: Ix | None = None) -> list[str]:
    """Разрешено только: ComputeBudget 2/3/4 без счетов; ATA Create/CreateIdempotent (раскладка ≥ 6 счетов; владелец,
    mint, программа и адрес — preparation_fees); ровно одна инструкция роутера по ВСЕМ спискам (если задана —
    это именно она; аргументы и счета сверяет адаптер по IDL); System Transfer — только как tip-инструкция и только
    от нашего кошелька (получатель — allowlist роутера). Всё прочее (Approve, SetAuthority, CloseAccount, Transfer
    токенов, второй своп, чужая программа, роутер другого провайдера) — «ix_not_in_manifest:<программа>:<дискр.>»."""
    r: list[str] = []
    routers = [ix for ix in ixs if ix.program_id == router_program]
    if len(routers) != 1 or (router_ix is not None and routers[0] is not router_ix):
        r.append(f"router_ix_count:{len(routers)}")
    for ix in ixs:
        p = ix.program_id
        if p == router_program:
            continue
        if p == COMPUTE_BUDGET_PROGRAM and ix.data and ix.data[0] in CB_ALLOWED and not ix.accounts:
            continue
        if p == ATA_PROGRAM and ix.data in ATA_ALLOWED and len(ix.accounts) >= 6:
            continue
        if (p == SYSTEM_PROGRAM and tip_ix is not None and ix is tip_ix and len(ix.data) == 12
                and int.from_bytes(ix.data[:4], "little") == 2 and len(ix.accounts) >= 2
                and ix.accounts[0].pubkey == req.wallet):
            continue
        r.append(f"ix_not_in_manifest:{PROGRAM_NAMES.get(p, p)}:{ix_disc(ix)}")   # чужая — полным адресом
    return r


@dataclass(frozen=True)
class Payload:
    """Что подписывать. kind: tx (готовая транзакция) | message | instructions (сборка у нас); encoding — явно."""
    kind: str
    encoding: str
    data: str = ""
    ixs: tuple[Ix, ...] = ()
    alts: tuple[tuple[str, tuple[str, ...]], ...] = ()   # адрес ALT → содержимое (пусто: раскрывает сборщик по RPC)

    def __post_init__(self):
        if self.kind in ("tx", "message"):
            if self.encoding not in ("base64", "base58") or not self.data or self.ixs:
                raise PayloadError(f"{self.kind}: нужна строка base64/base58")
        elif self.kind == "instructions":
            if self.encoding != "json" or not self.ixs or self.data:
                raise PayloadError("instructions: нужен непустой список инструкций")
        else:
            raise PayloadError(f"payload_kind {self.kind!r}")

    def raw_bytes(self) -> bytes:
        if self.kind == "instructions":
            raise PayloadError("у списка инструкций нет байтов транзакции: сначала сборка")
        return decode_bytes(self.data, self.encoding)


# --- граница с Solana-ядром (ждёт solders): сборка, симуляция, полная проверка ----------------------------------
@dataclass(frozen=True)
class UnsignedTx:
    """Собранная неподписанная транзакция: ровно эти байты проверены и будут подписаны (message_hash закреплён)."""
    payload: Payload
    message_hash: str
    signers: tuple[str, ...]
    recent_blockhash: str
    last_valid_block_height: int | None
    cu_limit: int | None


@dataclass(frozen=True)
class SimResult:
    ok: bool
    err: str | None
    units_consumed: int | None
    slot: int | None


@runtime_checkable
class TxAssembler(Protocol):
    """Ждёт solders: v0-message с раскрытыми по RPC ALT (владелец таблицы, не деактивирована), наш payer.
    cu_limit=N — добавить ровно одну SetComputeUnitLimit(N) (цикл CU Build); cu_limit=None — не добавлять
    (у OKX лимит уже в инструкциях). message_hash — sha256 сериализованного message; подписываются только эти байты.
    wrap() — готовая транзакция провайдера как есть, без модификации (Order, сверка OKX /swap)."""
    def assemble(self, *, payer: str, ixs: Sequence[Ix], alts: Sequence[tuple[str, tuple[str, ...]]],
                 recent_blockhash: str, last_valid_block_height: int, cu_limit: int | None) -> UnsignedTx: ...
    def wrap(self, payload: Payload, last_valid_block_height: int | None) -> UnsignedTx: ...   # готовая tx как есть


@runtime_checkable
class Simulator(Protocol):
    """simulateTransaction точного message: sigVerify=false, replaceRecentBlockhash=false."""
    def simulate(self, tx: UnsignedTx) -> SimResult: ...


@runtime_checkable
class ChainState(Protocol):
    def latest_blockhash(self) -> tuple[str, int]: ...          # hash и ЕГО lastValidBlockHeight (одна пара)
    def block_height(self) -> int | None: ...


@runtime_checkable
class PayloadValidator(Protocol):
    """Ждёт solders: полный message + ALT против манифеста программ/раскладок (§9.1). () — прошло.
    Мост к solana/validate.TransactionValidator — trade/sol_route_validator.RouteTxValidator."""
    def validate(self, tx: UnsignedTx, cand: "SwapCandidate", req: QuoteRequest) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class SolanaTools:
    assembler: TxAssembler | None = None
    simulator: Simulator | None = None
    chain: ChainState | None = None
    validator: PayloadValidator | None = None

    def can_build(self) -> bool:
        return self.assembler is not None and self.simulator is not None and self.chain is not None


# --- политика и лимиты владельца ------------------------------------------------------------------------------
def _opt_int(d: Mapping, k: str) -> int | None:
    v = d.get(k)
    if v is None:
        return None
    if isinstance(v, bool) or isinstance(v, float):
        raise ValueError(f"{k}: целое, а не {type(v).__name__}")
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if not isinstance(v, int) or v < 0:
        raise ValueError(f"{k}: целое ≥ 0, а не {v!r}")
    return v


def _opt_dec(d: Mapping, k: str) -> D | None:
    v = d.get(k)
    if v is None:
        return None
    x = dec_str(v, k)
    if x < 0:
        raise ValueError(f"{k}: отрицательное")
    return x


def _opt_bool(d: Mapping, k: str, default: bool) -> bool:
    v = d.get(k)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise ValueError(f"{k}: true/false, а не {v!r}")
    return v


@dataclass(frozen=True)
class RoutingPolicy:
    """[routing.solana] + возможности кода. Значения по умолчанию — безопасная сторона (всё спорное выключено)."""
    paths: tuple[str, ...] = PATHS
    require_both_providers_for_entry: bool = True
    allow_degraded_entry: bool = False
    allow_single_provider_risk_reducing_exit: bool = False
    allow_external_signer_managed_routes: bool = False
    order_execution_enabled: bool = False     # исполнение Jupiter Order (/execute) в первом live не реализовано
    external_signer_recovery: bool = False    # восстановление по requestId при внешнем payer не доказано (G14)
    max_collection_rounds: int = 2
    tip_recipients: tuple[str, ...] = ()      # кому можно платить tip (tx.jup.ag/Jito — по реестру владельца); пусто — никому

    def __post_init__(self):
        bad = [p for p in self.paths if p not in PATHS]
        if bad:
            raise ValueError(f"неизвестные пути: {bad}")
        if not 1 <= self.max_collection_rounds <= 2:
            raise ValueError("max_collection_rounds: 1 или 2 (одна повторная сессия, §5.1 п.7)")
        if isinstance(self.tip_recipients, str) or not all(is_pubkey(a) for a in self.tip_recipients):
            raise ValueError("tip_recipients: список base58-адресов (регистр значим)")

    @classmethod
    def from_config(cls, d: Mapping) -> "RoutingPolicy":
        paths = d.get("paths")
        tips = d.get("tip_recipients")
        if tips is not None and not isinstance(tips, (list, tuple)):
            raise ValueError("tip_recipients: список адресов")
        return cls(paths=tuple(paths) if paths is not None else PATHS,
                   tip_recipients=tuple(tips) if tips is not None else (),
                   require_both_providers_for_entry=_opt_bool(d, "require_both_providers_for_entry", True),
                   allow_degraded_entry=_opt_bool(d, "allow_degraded_entry", False),
                   allow_single_provider_risk_reducing_exit=_opt_bool(d, "allow_single_provider_risk_reducing_exit", False),
                   allow_external_signer_managed_routes=_opt_bool(d, "allow_external_signer_managed_routes", False),
                   max_collection_rounds=_opt_int(d, "max_collection_rounds") or 2)


@dataclass(frozen=True)
class RouteLimits:
    """Лимиты владельца, которые проверяет роутер. None — значение не задано: live запрещён (limit_missing)."""
    max_quote_age_ms: int | None = None
    collection_deadline_ms: int | None = None
    min_blockhash_validity_heights: int | None = None
    max_network_fee_lamports_per_tx: int | None = None
    max_tip_lamports_per_tx: int | None = None        # нет значения — любой tip > 0 запрещён
    max_spot_slippage_bps: int | None = None
    max_price_impact_bps: int | None = None
    max_native_price_age_ms: int | None = None
    max_book_age_ms: int | None = None
    max_rent_locked_lamports: int | None = None
    min_route_improvement_usdc: D | None = None       # гистерезис — только если задан явно

    @classmethod
    def from_config(cls, d: Mapping) -> "RouteLimits":
        ints = ("max_quote_age_ms", "collection_deadline_ms", "min_blockhash_validity_heights",
                "max_network_fee_lamports_per_tx", "max_tip_lamports_per_tx", "max_spot_slippage_bps",
                "max_price_impact_bps", "max_native_price_age_ms", "max_book_age_ms", "max_rent_locked_lamports")
        return cls(**{k: _opt_int(d, k) for k in ints},
                   min_route_improvement_usdc=_opt_dec(d, "min_route_improvement_usdc"))


# --- кандидат -------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class SwapCandidate:
    """Нормализованный кандидат одного пути (SOLANA_ROUTERS §3). Неизвестное — None; reasons пуст — пригоден."""
    provider: str                       # jupiter | okx
    path: str
    adapter_version: str
    request_hash: str
    side: str
    input_mint: str
    output_mint: str
    input_program: str
    output_program: str
    input_decimals: int
    output_decimals: int
    amount_in_raw: int
    expected_out_raw: int | None
    min_out_raw: int | None             # порог из JSON провайдера (сверка)
    onchain_min_out_raw: int | None     # порог из аргументов инструкции роутера (то, что исполнит программа)
    fees: tuple[FeeComponent, ...] = ()
    payload: Payload | None = None
    required_signers: tuple[str, ...] = ()
    recent_blockhash: str | None = None
    last_valid_block_height: int | None = None
    rfq_expire_at: float | None = None
    message_hash: str | None = None
    cu_limit: int | None = None
    cu_price_micro: int | None = None
    price_impact_bps: D | None = None
    route_fingerprint: str = ""
    request_id: str | None = None
    quote_id: str | None = None
    received_at: float = 0.0            # время ответа по стенным часам (UTC epoch)
    received_mono: float = 0.0
    built_mono: float | None = None     # время финальной сборки/симуляции
    provider_time: float | None = None  # время источника, если провайдер его дал; None — не дал (видно явно)
    source_slot: int | None = None
    latency_ms: int | None = None
    simulation_ok: bool | None = None
    sim_units: int | None = None
    sim_slot: int | None = None
    validated: bool | None = None
    reasons: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    response_hash: str = ""
    audit: tuple[tuple[str, str], ...] = ()

    @property
    def group(self) -> str:
        return GROUP_OF[self.path]

    @property
    def effective_min_out(self) -> int | None:
        return self.onchain_min_out_raw if self.onchain_min_out_raw is not None else self.min_out_raw

    @property
    def fresh_mono(self) -> float:
        return self.built_mono if self.built_mono is not None else self.received_mono

    @property
    def hard_reasons(self) -> tuple[str, ...]:
        return tuple(r for r in self.reasons if not live_only(r))

    def with_reasons(self, *r: str) -> "SwapCandidate":
        return replace(self, reasons=tuple(dict.fromkeys(self.reasons + tuple(r))))

    def with_notes(self, *n: str) -> "SwapCandidate":
        return replace(self, notes=tuple(dict.fromkeys(self.notes + tuple(n))))


@dataclass(frozen=True)
class Unavailable:
    """Путь не дал кандидата: таймаут, нет ключа, нет маршрута, ошибка схемы. Не «цена 0»."""
    provider: str
    path: str
    reason: str
    detail: str = ""
    at_mono: float = 0.0

    @property
    def group(self) -> str:
        return GROUP_OF.get(self.path, self.provider)


# --- экономика пары ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class PairParams:
    fs: D                                # единиц underlying в споте
    fp: D                                # единиц underlying в контракте
    step: D                              # шаг количества перпа
    perp_fee_rate: D | None              # применимая ставка HL (с HIP-3 множителем); None — неизвестна
    min_notional: D | None
    exit_close_qty: D | None = None      # выход: сколько контрактов закрываем (из фактической позиции)
    # маржа входа (§5.1 п.5, §5.3): плечо и резерв — только владелец; доступная маржа — состояние счёта HL на нужном
    # ledger (Standard: para; Unified — из спотового USDC) от адаптера HL. None — не задано/неизвестно: live нельзя
    leverage: D | None = None
    margin_reserve: D | None = None      # USDC, остаётся свободным после входа
    available_margin: D | None = None    # USDC, доступно под изолированную позицию

    def __post_init__(self):
        for k in ("leverage", "margin_reserve", "available_margin"):
            v = getattr(self, k)
            if v is not None and (not isinstance(v, D) or not v.is_finite() or v < 0):
                raise ValueError(f"{k}: Decimal ≥ 0, а не {v!r}")
        if self.leverage is not None and self.leverage <= 0:
            raise ValueError("leverage: > 0")


@dataclass(frozen=True)
class HedgeContext:
    book: Book | None
    params: PairParams
    now_wall: float | None = None        # когда контекст собран — только для журнала; возраст стакана меряется
                                         # часами оценки (evaluate/rank), а не этим полем


@dataclass(frozen=True)
class PairCheck:
    qty: D | None = None
    vwap: D | None = None
    notional: D | None = None
    fee: D | None = None
    edge: D | None = None                # денежная разница пары на входе/выходе (не заработанный PnL)
    basis_bps: D | None = None
    margin_needed: D | None = None       # вход: маржа под шорт ожидаемого объёма (с комиссией), USDC
    margin_at_min_out: D | None = None   # то же при минимальном выходе
    reasons: tuple[str, ...] = ()


def floor_step(x: D, step: D) -> D:
    if step <= 0:
        raise ValueError("шаг перпа ≤ 0")
    return (x // step) * step


def walk_book(levels: Sequence[tuple[D, D]], qty: D) -> tuple[D, D] | None:
    """VWAP на ВЕСЬ объём по уровням; глубины не хватает — None (последний уровень не продолжается бесконечно)."""
    left, notional = qty, D(0)
    for px, sz in levels:
        take = min(left, sz)
        notional += take * px
        left -= take
        if left <= 0:
            return notional / qty, notional
    return None


def margin_for(q: D, px_ref: D, lev: D, fee: D | None) -> D:
    """Маржа изолированного шорта: q × опорная цена / плечо + комиссия (если известна)."""
    return q * px_ref / lev + (fee if fee is not None else 0)


def pair_check(c: SwapCandidate, req: QuoteRequest, hedge: HedgeContext, limits: RouteLimits,
               spot_value: D | None, *, now_wall: float) -> PairCheck:
    """now_wall — часы момента оценки: стакан, собранный до долгого сбора котировок, к оценке уже мог протухнуть."""
    p, b, r = hedge.params, hedge.book, []
    if b is None:
        return PairCheck(reasons=("hedge_book_unavailable",))
    if limits.max_book_age_ms is None:
        r.append("limit_missing:max_book_age_ms")
    elif (now_wall - b.ts) * 1000 > limits.max_book_age_ms:
        r.append("book_stale")
    if req.side == "entry":
        if c.expected_out_raw is None:
            return PairCheck(reasons=tuple(r + ["hedge_qty_unknown"]))
        tokens = human(c.expected_out_raw, c.output_decimals)
        q, levels = floor_step(tokens * p.fs / p.fp, p.step), b.bids
    else:
        tokens = human(c.amount_in_raw, c.input_decimals)
        q = p.exit_close_qty if p.exit_close_qty is not None else floor_step(tokens * p.fs / p.fp, p.step)
        levels = b.asks
    if q <= 0:
        return PairCheck(qty=q, reasons=tuple(r + ["hedge_qty_zero"]))
    w = walk_book(levels, q)
    if w is None:
        return PairCheck(qty=q, reasons=tuple(r + ["hedge_depth"]))
    vwap, notional = w
    if p.min_notional is not None and notional < p.min_notional:
        r.append("hedge_below_min_notional")
    q_min = wm = None
    if req.side == "entry" and c.effective_min_out is not None:
        q_min = floor_step(human(c.effective_min_out, c.output_decimals) * p.fs / p.fp, p.step)
        wm = walk_book(levels, q_min) if q_min > 0 else None
    if req.side == "entry" and p.min_notional is not None and c.effective_min_out is not None:
        if wm is None or wm[1] < p.min_notional:
            r.append("hedge_below_min_notional_at_min_out")
    fee = notional * p.perp_fee_rate if p.perp_fee_rate is not None else None
    if fee is None:
        r.append("perp_fee_unknown")
    m_need = m_min = None
    if req.side == "entry":             # выход закрывает шорт (reduce-only) — маржи не просит
        # опорная цена — лучший аск (выше mid и цены продажи): маржу HL считает не от нашего VWAP по бидам
        px_ref = max(b.asks[0][0], levels[0][0]) if b.asks else levels[0][0]
        if p.leverage is None:
            r.append("limit_missing:hl_leverage")
        else:
            m_need = margin_for(q, px_ref, p.leverage, fee)
            if wm is not None:
                m_min = margin_for(q_min, px_ref, p.leverage,
                                   wm[1] * p.perp_fee_rate if p.perp_fee_rate is not None else None)
        if p.margin_reserve is None:
            r.append("limit_missing:hl_margin_reserve")
        if p.available_margin is None:
            r.append("margin_unknown")
        if m_need is not None and p.margin_reserve is not None and p.available_margin is not None:
            if m_need + p.margin_reserve > p.available_margin:
                r.append("margin_insufficient")     # SOLANA_ROUTERS сценарий 7: новый вход не исполняется
    edge = basis = None
    if spot_value is not None and fee is not None and tokens > 0 and spot_value > 0:
        edge = (notional - spot_value - fee) if req.side == "entry" else (spot_value - notional - fee)
        basis = ((vwap / p.fp) / (spot_value / (tokens * p.fs)) - 1) * 10000
    return PairCheck(qty=q, vwap=vwap, notional=notional, fee=fee, edge=edge, basis_bps=basis, margin_needed=m_need,
                     margin_at_min_out=m_min, reasons=tuple(r))


# --- оценка и ранжирование --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Ranked:
    cand: SwapCandidate
    metric: D | None               # вход: стоимость за токен; выход: чистые USDC
    conservative: D | None         # то же по минимальному выходу
    valuation: Valuation | None
    pair: PairCheck | None
    eligible: bool
    previewable: bool
    rank: int | None = None


def time_reasons(c: SwapCandidate, limits: RouteLimits, now_mono: float, now_wall: float,
                 block_height: int | None) -> list[str]:
    r = []
    if limits.max_quote_age_ms is None:
        r.append("limit_missing:max_quote_age_ms")
    elif (now_mono - c.fresh_mono) * 1000 > limits.max_quote_age_ms:
        r.append("stale")
    if c.rfq_expire_at is not None and c.rfq_expire_at <= now_wall:
        r.append("rfq_expired")
    if c.message_hash is not None:
        if c.last_valid_block_height is None:
            r.append("blockhash_unknown")
        elif block_height is None:
            r.append("block_height_unknown")
        elif limits.min_blockhash_validity_heights is None:
            r.append("limit_missing:min_blockhash_validity_heights")
        elif c.last_valid_block_height - block_height < limits.min_blockhash_validity_heights:
            r.append("blockhash_expiring")
    return r


def evaluate(c: SwapCandidate, req: QuoteRequest, *, policy: RoutingPolicy, limits: RouteLimits,
             prices: Mapping[str, PriceObs], now_mono: float, now_wall: float, block_height: int | None = None,
             hedge: HedgeContext | None = None) -> Ranked:
    r: list[str] = []
    if c.path not in policy.paths:
        r.append(f"path_disabled:{c.path}")
    if c.request_hash != req.request_hash:
        r.append("request_mismatch")
    for got, want, what in ((c.side, req.side, "side"), (c.input_mint, req.input.mint, "input_mint"),
                            (c.output_mint, req.output.mint, "output_mint"), (c.amount_in_raw, req.amount_in_raw, "amount"),
                            (c.input_program, req.input.program, "input_program"),
                            (c.output_program, req.output.program, "output_program"),
                            (c.input_decimals, req.input.decimals, "input_decimals"),
                            (c.output_decimals, req.output.decimals, "output_decimals")):
        if got != want:
            r.append(f"request_mismatch:{what}")
    exp, mo = c.expected_out_raw, c.effective_min_out
    if not exp:
        r.append("no_expected_out")
    if not mo:
        r.append("no_min_out")
    if c.min_out_raw is not None and c.onchain_min_out_raw is not None and c.min_out_raw != c.onchain_min_out_raw:
        r.append("min_out_mismatch")                           # R09: JSON говорит одно, инструкция — другое
    if exp and mo:
        if mo > exp:
            r.append("min_out_above_expected")
        if mo < exp * (10000 - req.slippage_bps) // 10000:
            r.append("min_out_below_slippage")
    if limits.max_spot_slippage_bps is None:
        r.append("limit_missing:max_spot_slippage_bps")
    elif req.slippage_bps > limits.max_spot_slippage_bps:
        r.append("slippage_over_limit")
    if c.price_impact_bps is None:
        r.append("price_impact_unknown")
    elif limits.max_price_impact_bps is None:
        r.append("limit_missing:max_price_impact_bps")
    elif abs(c.price_impact_bps) > limits.max_price_impact_bps:
        r.append("price_impact_over_limit")
    if limits.collection_deadline_ms is None:
        r.append("limit_missing:collection_deadline_ms")
    r += time_reasons(c, limits, now_mono, now_wall, block_height)
    if c.simulation_ok is None:
        r.append("not_simulated")
    if c.validated is None:
        r.append("not_validated")
    # расходы: только наши и только сверх потоков токенов
    val = value_external(c.fees, wallet=req.wallet, unit=req.unit_mint, prices=prices, now_mono=now_mono,
                         max_price_age_ms=limits.max_native_price_age_ms)
    r += [f"fee_unknown:{u}" for u in val.unknown]
    if val.unchecked:
        r.append("limit_missing:max_native_price_age_ms")
    own = [f for f in c.fees if f.asset == NATIVE_SOL and not f.superseded and f.payer in (req.wallet, None)]
    net = [f.amount_raw for f in own if f.kind in ("network_base", "network_priority", "network_total")]
    if limits.max_network_fee_lamports_per_tx is None:
        r.append("limit_missing:max_network_fee_lamports_per_tx")
    elif None not in net and sum(net) > limits.max_network_fee_lamports_per_tx:
        r.append("network_fee_over_cap")
    tips = [f.amount_raw for f in own if f.kind == "tip"]
    if None not in tips and sum(tips) > 0:
        if limits.max_tip_lamports_per_tx is None:
            r.append("tip_forbidden")                         # R14: нет лимита владельца — tip запрещён
        elif sum(tips) > limits.max_tip_lamports_per_tx:
            r.append("tip_over_cap")
    # R14: получатель tip — только из allowlist владельца; не разобран или вне списка — отказ при любой сумме
    if any(f.recipient is None or f.recipient not in policy.tip_recipients
           for f in c.fees if f.kind == "tip" and not f.superseded):
        r.append("tip_recipient_unknown")
    locked = native_split(c.fees, req.wallet).rent_locked
    if locked is None:
        r.append("rent_unknown")
    elif locked > 0:
        if limits.max_rent_locked_lamports is None:
            r.append("limit_missing:max_rent_locked_lamports")
        elif locked > limits.max_rent_locked_lamports:
            r.append("rent_over_cap")
    metric = cons = spot_value = None
    if exp and val.total is not None:
        with localcontext() as ctx:
            ctx.prec = 50
            if req.side == "entry":
                spot_value = human(c.amount_in_raw, c.input_decimals) + val.total
                metric = spot_value / human(exp, c.output_decimals)
                cons = spot_value / human(mo, c.output_decimals) if mo else None
            else:
                spot_value = human(exp, c.output_decimals) - val.total
                metric = spot_value
                cons = human(mo, c.output_decimals) - val.total if mo else None
    pc = None
    if hedge is None:
        r.append("hedge_unchecked")
    else:
        pc = pair_check(c, req, hedge, limits, spot_value, now_wall=now_wall)
        r += list(pc.reasons)
    if metric is None and not any(x.startswith(("fee_unknown", "no_expected_out")) for x in r):
        r.append("metric_unknown")
    cand = c.with_reasons(*r)
    eligible = not cand.reasons and metric is not None
    previewable = metric is not None and all(live_only(x) for x in cand.reasons)
    return Ranked(cand, metric, cons, val, pc, eligible, previewable)


def _path_order(p: str) -> int:
    return PATHS.index(p) if p in PATHS else len(PATHS)


def rank(cands: Sequence[SwapCandidate], req: QuoteRequest, *, policy: RoutingPolicy, limits: RouteLimits,
         prices: Mapping[str, PriceObs], now_mono: float, now_wall: float, block_height: int | None = None,
         hedge: HedgeContext | None = None, incumbent_path: str | None = None
         ) -> tuple[tuple[Ranked, ...], tuple[str, ...]]:
    """Все кандидаты с метрикой и причинами; пригодные — по возрастанию стоимости входа / убыванию выручки выхода."""
    ev = [evaluate(c, req, policy=policy, limits=limits, prices=prices, now_mono=now_mono, now_wall=now_wall,
                   block_height=block_height, hedge=hedge) for c in cands]
    sign = 1 if req.side == "entry" else -1

    def key(x: Ranked):
        m = x.metric if x.metric is not None else D(0)
        cv = x.conservative if x.conservative is not None else m
        return (0 if x.metric is not None else 1, sign * m, sign * cv, _path_order(x.cand.path))

    elig = sorted([x for x in ev if x.eligible], key=key)
    warnings: list[str] = []
    if len(elig) >= 2:
        by_cons = min(elig, key=lambda x: (sign * x.conservative, _path_order(x.cand.path)))
        if by_cons.cand.path != elig[0].cand.path:
            warnings.append(f"rank_unstable_within_slippage:{elig[0].cand.path}/{by_cons.cand.path}")
    if incumbent_path and limits.min_route_improvement_usdc is not None and len(elig) >= 2:
        inc = next((x for x in elig if x.cand.path == incumbent_path), None)
        if inc is not None and inc is not elig[0]:
            with localcontext() as ctx:
                ctx.prec = 50
                gain = ((inc.metric - elig[0].metric) * human(elig[0].cand.expected_out_raw, elig[0].cand.output_decimals)
                        if req.side == "entry" else elig[0].metric - inc.metric)
            if gain < limits.min_route_improvement_usdc:
                elig.remove(inc)
                elig.insert(0, inc)
                warnings.append(f"hysteresis_kept:{incumbent_path}")
    rest = sorted([x for x in ev if not x.eligible], key=key)
    out = tuple(replace(x, rank=i + 1) for i, x in enumerate(elig)) + tuple(rest)
    return out, tuple(warnings)


@dataclass(frozen=True)
class Decision:
    """Итог сбора. status: complete (обе группы) | degraded (вход по разрешению владельца) | single_provider
    (выход по разрешённой политике) | refused. winner — только пригодный; preview_winner — лучший для показа."""
    side: str
    status: str
    winner: SwapCandidate | None
    preview_winner: SwapCandidate | None
    ranked: tuple[Ranked, ...]
    unavailable: tuple[Unavailable, ...]
    groups_eligible: tuple[str, ...]
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    note: str
    round_no: int
    request_hash: str
    metric_version: str = METRIC_VERSION

    @property
    def winner_ranked(self) -> Ranked | None:
        return next((x for x in self.ranked if self.winner is not None and x.cand is self.winner), None)

    def records(self, operation_id: str, clip_seq: int) -> list[dict]:
        """Строки route_candidates (§12): все кандидаты, причины исключения, времена, выбранный, хеш ответа."""
        rows = []
        for x in self.ranked:
            c = x.cand
            cid = hashlib.sha256(f"{c.request_hash}|{c.path}|{c.received_mono!r}|{c.response_hash}".encode()).hexdigest()[:16]
            s = lambda v: None if v is None else str(v)       # noqa: E731
            rows.append(dict(
                operation_id=operation_id, clip_seq=clip_seq, candidate_id=cid, round_no=self.round_no,
                provider=c.provider, path=c.path, group=c.group, route_fingerprint=c.route_fingerprint,
                request_hash=c.request_hash, side=c.side, amount_in_raw=str(c.amount_in_raw),
                expected_out_raw=s(c.expected_out_raw), min_out_raw=s(c.min_out_raw),
                onchain_min_out_raw=s(c.onchain_min_out_raw), metric=s(x.metric), conservative=s(x.conservative),
                external_cost=s(x.valuation.total if x.valuation else None), fees=[f.as_record() for f in c.fees],
                pair_edge=s(x.pair.edge if x.pair else None), pair_basis_bps=s(x.pair.basis_bps if x.pair else None),
                pair_margin=s(x.pair.margin_needed if x.pair else None),
                received_at=c.received_at, provider_time=c.provider_time, latency_ms=c.latency_ms,
                built=c.built_mono is not None, message_hash=c.message_hash, request_id=c.request_id,
                reasons=list(c.reasons), notes=list(c.notes), rank=x.rank, eligible=x.eligible,
                selected=self.winner is not None and c is self.winner,
                preview_selected=self.preview_winner is not None and c is self.preview_winner,
                response_hash=c.response_hash, metric_version=self.metric_version, decision=self.status))
        for u in self.unavailable:
            rows.append(dict(operation_id=operation_id, clip_seq=clip_seq, candidate_id=None, round_no=self.round_no,
                             provider=u.provider, path=u.path, group=u.group, reasons=[u.reason], notes=[u.detail],
                             selected=False, eligible=False, metric_version=self.metric_version,
                             decision=self.status))
        return rows


def gate(ranked: Sequence[Ranked], unavailable: Sequence[Unavailable], req: QuoteRequest, policy: RoutingPolicy,
         warnings: Sequence[str] = (), round_no: int = 1) -> Decision:
    """Ворота групп ПОСЛЕ всех проверок (§5.2): HTTP 200 и неподписываемая котировка группу не засчитывают."""
    elig = [x for x in ranked if x.eligible]
    prev = next((x.cand for x in sorted([x for x in ranked if x.previewable],
                                        key=lambda x: ((1 if req.side == "entry" else -1) * x.metric,
                                                       _path_order(x.cand.path)))), None)
    groups = tuple(g for g in GROUPS if any(x.cand.group == g for x in elig))
    missing = [g for g in GROUPS if g not in groups]
    why = "; ".join(f"{u.path}: {u.reason}" for u in unavailable if u.group in missing)
    base = dict(side=req.side, preview_winner=prev, ranked=tuple(ranked), unavailable=tuple(unavailable),
                groups_eligible=groups, warnings=tuple(warnings), round_no=round_no, request_hash=req.request_hash)
    if not elig:
        return Decision(status="refused", winner=None, reasons=("no_eligible_route",),
                        note="нет проверенного маршрута — отправки нет", **base)
    winner = elig[0].cand
    note = ""
    if missing:
        note = f"выбран лучший из доступных; нет проверенного пути группы {', '.join(missing)}" + (f" ({why})" if why else "")
    if req.side == "entry":
        if missing and policy.require_both_providers_for_entry:
            if not policy.allow_degraded_entry:
                return Decision(status="refused", winner=None,
                                reasons=(f"incomplete_comparison:{','.join(missing)}",), note=note, **base)
            return Decision(status="degraded", winner=winner, reasons=(), note=note, **base)
        return Decision(status="single_provider" if missing else "complete", winner=winner, reasons=(), note=note, **base)
    if missing:
        if not policy.allow_single_provider_risk_reducing_exit:
            return Decision(status="refused", winner=None, reasons=(f"incomplete_comparison:{','.join(missing)}",),
                            note=note, **base)
        return Decision(status="single_provider", winner=winner, reasons=(), note=note, **base)
    return Decision(status="complete", winner=winner, reasons=(), note="", **base)


def _refused(req: QuoteRequest, reason: str, note: str, round_no: int = 0) -> Decision:
    return Decision(side=req.side, status="refused", winner=None, preview_winner=None, ranked=(), unavailable=(),
                    groups_eligible=(), reasons=(reason,), warnings=(), note=note, round_no=round_no,
                    request_hash=req.request_hash)


# --- квоты провайдеров -------------------------------------------------------------------------------------------
class RateGate:
    """Квота провайдера в процессе: не больше одного запроса в полёте, интервал между стартами, очередь по
    приоритету (recovery → exit → entry → mark → scanner). Слот, который не успевает к сроку, не занимается."""

    def __init__(self, min_gap_s: float):
        if not min_gap_s >= 0:
            raise ValueError("min_gap_s ≥ 0")
        self.min_gap_s = min_gap_s
        self._cv = threading.Condition()
        self._q: list[tuple[int, int]] = []
        self._seq = itertools.count()
        self._busy = False
        self._last = -1e18

    def _acquire(self, purpose: str, deadline_mono: float) -> bool:
        me = (PURPOSES.index(purpose), next(self._seq))
        with self._cv:
            heapq.heappush(self._q, me)
            granted = False
            try:
                while True:
                    now = time.monotonic()
                    if now >= deadline_mono:
                        return False
                    if not self._busy and self._q[0] == me:
                        wait_s = self._last + self.min_gap_s - now
                        if wait_s <= 0:
                            heapq.heappop(self._q)
                            self._busy, self._last, granted = True, now, True
                            return True
                        if now + wait_s > deadline_mono:
                            return False
                        self._cv.wait(timeout=wait_s)
                    else:
                        self._cv.wait(timeout=max(0.0, deadline_mono - now))
            finally:
                if not granted:
                    if me in self._q:
                        self._q.remove(me)
                        heapq.heapify(self._q)
                    self._cv.notify_all()

    def _release(self) -> None:
        with self._cv:
            self._busy = False
            self._cv.notify_all()

    @contextmanager
    def slot(self, purpose: str, deadline_mono: float):
        ok = self._acquire(purpose, deadline_mono)
        try:
            yield ok
        finally:
            if ok:
                self._release()


# --- роутер ---------------------------------------------------------------------------------------------------
@runtime_checkable
class RouteProvider(Protocol):
    group: str
    paths: tuple[str, ...]

    def candidates(self, req: QuoteRequest) -> list: ...      # [SwapCandidate | Unavailable]


class SpotRouter:
    """Сбор с единым сроком (провайдеры параллельно, пути внутри провайдера — последовательно), оценка, ворота,
    одна повторная сессия. Не подписывает, не отправляет, не держит кошелёк."""

    def __init__(self, providers: Sequence[RouteProvider], *, policy: RoutingPolicy = RoutingPolicy(),
                 limits: RouteLimits = RouteLimits(), clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time):
        self.providers = list(providers)
        self.policy, self.limits, self.clock, self.wall = policy, limits, clock, wall

    def collect(self, req: QuoteRequest) -> tuple[list[SwapCandidate], list[Unavailable]]:
        active = [p for p in self.providers if any(x in self.policy.paths for x in p.paths)]
        cands: list[SwapCandidate] = []
        unav: list[Unavailable] = []
        if not active:
            return cands, unav
        ex = ThreadPoolExecutor(max_workers=len(active), thread_name_prefix="sol-route")
        futs = {ex.submit(p.candidates, req): p for p in active}
        done, not_done = _fwait(futs, timeout=max(0.0, req.deadline_mono - self.clock()))
        for f in done:
            p = futs[f]
            try:
                for x in f.result() or []:
                    (cands if isinstance(x, SwapCandidate) else unav).append(x)
            except Exception as e:        # noqa — сбой адаптера = путь недоступен, а не падение выбора
                log.warning("spot_router: %s упал: %s", p.group, type(e).__name__)
                unav += [Unavailable(p.group, path, "error", type(e).__name__, self.clock()) for path in p.paths]
        for f in not_done:
            p = futs[f]
            unav += [Unavailable(p.group, path, "deadline", "нет ответа к сроку сбора", self.clock()) for path in p.paths]
        ex.shutdown(wait=False, cancel_futures=True)
        return cands, unav

    def select(self, req: QuoteRequest, *, prices: Mapping[str, PriceObs],
               block_height: Callable[[], int | None] | int | None = None,
               hedge: Callable[[], HedgeContext | None] | HedgeContext | None = None,
               inflight_unknown: Callable[[], str | None] | None = None, incumbent_path: str | None = None) -> Decision:
        """hedge — лучше колбэк: стакан HL берётся ПОСЛЕ сбора котировок каждого раунда (свежий к оценке). Готовый
        контекст оценивается часами момента оценки; протухший стакан повтором сбора тогда не лечится."""
        if inflight_unknown is not None:
            why = inflight_unknown()
            if why:      # R12: пока исход прошлой отправки неизвестен, новый маршрут не выбирается и не строится
                return _refused(req, f"unknown_inflight:{why}", "сначала разрешить исход прошлой отправки")
        span = (self.limits.collection_deadline_ms / 1000 if self.limits.collection_deadline_ms is not None
                else max(0.0, req.deadline_mono - self.clock()))
        rounds = self.policy.max_collection_rounds
        dec = None
        for i in range(1, rounds + 1):
            r = req if i == 1 and self.limits.collection_deadline_ms is None else replace(req, deadline_mono=self.clock() + span)
            cands, unav = self.collect(r)
            bh = block_height() if callable(block_height) else block_height
            h, hwarn = self._hedge(hedge)
            ranked, warns = rank(cands, r, policy=self.policy, limits=self.limits, prices=prices,
                                 now_mono=self.clock(), now_wall=self.wall(), block_height=bh, hedge=h,
                                 incumbent_path=incumbent_path)
            dec = gate(ranked, unav, r, self.policy, warns + hwarn, i)
            if dec.status != "refused" or not self._transient(dec, fresh_book=callable(hedge)):
                return dec
        if rounds > 1:
            dec = replace(dec, reasons=dec.reasons + ("plan_expired",),
                          note=(dec.note + "; " if dec.note else "") + "повторная сессия не дала маршрута — план истёк")
        return dec

    @staticmethod
    def _hedge(hedge) -> tuple[HedgeContext | None, tuple[str, ...]]:
        if not callable(hedge):
            return hedge, ()
        try:
            return hedge(), ()
        except Exception as e:        # noqa — стакан не получен: кандидаты без проверки пары (hedge_unchecked)
            log.warning("spot_router: стакан HL не получен: %s", type(e).__name__)
            return None, (f"hedge_fetch_error:{type(e).__name__}",)

    @staticmethod
    def _transient(dec: Decision, *, fresh_book: bool = False) -> bool:
        codes = ({code_of(r) for x in dec.ranked for r in x.cand.reasons} | {code_of(u.reason) for u in dec.unavailable}
                 | {code_of(w) for w in dec.warnings})
        if not fresh_book:              # тот же стакан во втором раунде свежее не станет
            codes -= {"book_stale", "hedge_fetch_error"}
        return bool(codes & TRANSIENT)

    def presign_check(self, dec: Decision, *, block_height: int | None) -> tuple[str, ...]:
        """Непосредственно перед подписью (§5.1 п.7): победитель ещё свеж, blockhash не истекает, RFQ не истёк."""
        if dec.winner is None:
            return ("no_winner",)
        return tuple(time_reasons(dec.winner, self.limits, self.clock(), self.wall(), block_height))


# --- замена маршрута после одобрения (R13) ----------------------------------------------------------------------
@dataclass(frozen=True)
class Approval:
    """Что одобрил владелец: кортеж инструмента (request_hash), политика и денежные границы клипа."""
    request_hash: str
    side: str
    policy: str                         # best | fixed:jupiter | fixed:okx
    provider_group: str
    min_out_raw: int
    max_cost_per_token: D | None = None     # вход
    min_net_usdc: D | None = None           # выход


def reselect_allowed(ap: Approval, dec: Decision) -> tuple[bool, tuple[str, ...], dict]:
    """Смена победителя после одобрения — только при policy=best, неизменном кортеже и в одобренных границах.
    Иначе — новое предложение владельцу. Итог и переход пишутся в аудит."""
    w = dec.winner_ranked
    if w is None:
        return False, ("no_winner",), {}
    r = []
    if dec.request_hash != ap.request_hash or dec.side != ap.side:
        r.append("tuple_changed")
    if ap.policy.startswith("fixed:"):
        if w.cand.group != ap.policy.split(":", 1)[1]:
            r.append("policy_fixed")
    elif ap.policy != "best":
        r.append("policy_unknown")
    if w.cand.effective_min_out is None or w.cand.effective_min_out < ap.min_out_raw:
        r.append("min_out_below_approved")
    if ap.side == "entry":
        if ap.max_cost_per_token is None:
            r.append("bound_missing")
        elif w.metric > ap.max_cost_per_token:
            r.append("cost_above_approved")
    else:
        if ap.min_net_usdc is None:
            r.append("bound_missing")
        elif w.metric < ap.min_net_usdc:
            r.append("net_below_approved")
    audit = dict(from_group=ap.provider_group, to_group=w.cand.group, to_path=w.cand.path, metric=str(w.metric),
                 min_out=str(w.cand.effective_min_out), policy=ap.policy, ok=not r, reasons=list(r))
    return not r, tuple(r), audit
