"""Полное сообщение Solana: раскрытие ALT по RPC и сборка MessageV0 (ТЗ §9.1, SOLANA_ROUTERS §6, S11).

Раскрытие (RpcMessageResolver.resolve):
  - байты сообщения разбираются ДВАЖДЫ — своим строгим разбором (wire.py) и solders; расхождение — отказ
    (разные парсеры не должны видеть разные транзакции);
  - каждая таблица ALT читается с узла (getMultipleAccounts base64): владелец AddressLookupTab1e…, тип LookupTable,
    не деактивирована, раскладка 56 байт + 32·n, сверка со вторым разбором (solders AddressLookupTable); адреса,
    дописанные в слоте чтения, ещё не активны; индекс вне активной части — отказ;
  - ключи в каноническом порядке: статические + загруженные writable (по порядку таблиц) + загруженные readonly;
    повтор ключа — отказ (рантайм тоже отвергнет);
  - writable — «может быть записан» (правило заголовка + загруженные writable), без понижения рантаймом: для
    проверки это надмножество, то есть осторожная сторона.
Кэш ALT (AltCache): версия = число адресов и sha256 содержимого. Таблица только дописывается, поэтому новое
чтение обязано продолжать старое; изменение уже виденных адресов — отказ и сброс записи, а не «молча другие счета».
Сборка (SoldersMessageBuilder.build_v0): solders MessageV0.try_compile со своим payer/blockhash и таблицами из
кэша (содержимое от провайдера не принимается на веру), затем обратный разбор своим парсером.

UNAVAILABLE — ничего не подключено: отказ (кандидат непригоден), а не «проверено».
"""
from __future__ import annotations
import base64, hashlib, struct, threading, time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence
from . import ALT_PROGRAM, WaitsSolders, wire
from .b58 import Base58Error, b58encode, check_pubkey

U64_MAX = 2 ** 64 - 1
ALT_META_SIZE = 56
ALT_TYPE_LOOKUP_TABLE = 1
MAX_ALT_ADDRESSES = 256


class MessageError(ValueError):
    """Полное сообщение не получено: ALT недоступна/чужая/деактивирована, парсеры расходятся, повтор ключа."""

    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code = code


@dataclass(frozen=True)
class AltSnapshot:
    address: str
    owner: str                          # обязан быть AddressLookupTab1e1111111111111111111111111
    deactivation_slot: int | None       # None = активна (u64::MAX в данных)
    last_extended_slot: int
    authority: str | None
    addresses: tuple[str, ...]
    slot: int                           # слот контекста чтения
    content_hash: str                   # sha256 списка адресов — в журнал (S11)
    last_extended_start: int = 0        # с этого индекса адреса дописаны в last_extended_slot

    @property
    def version(self) -> tuple[int, str]:
        return len(self.addresses), self.content_hash

    def active_len(self) -> int:
        """Сколько адресов уже можно использовать: дописанные в слоте чтения ещё не активны."""
        if self.last_extended_slot >= self.slot:
            return min(self.last_extended_start, len(self.addresses))
        return len(self.addresses)


@dataclass(frozen=True)
class ResolvedInstruction:
    program_id: str
    accounts: tuple[str, ...]
    data: bytes


@dataclass(frozen=True)
class ResolvedMessage:
    version: str | int
    payer: str
    signers: tuple[str, ...]
    writable: frozenset[str]
    readonly: frozenset[str]
    keys: tuple[str, ...]               # static + loaded writable + loaded readonly
    alts: tuple[AltSnapshot, ...]
    instructions: tuple[ResolvedInstruction, ...]
    recent_blockhash: str
    message_hash: str
    n_static: int | None = None         # сколько ключей статических (None — неизвестно: старый вызов)


class MessageResolver(Protocol):
    def resolve(self, tx: wire.WireTransaction) -> ResolvedMessage: ...


class MessageBuilder(Protocol):
    def build_v0(self, *, payer: str, instructions: Sequence[Any], recent_blockhash: str,
                 alts: Sequence[AltSnapshot]) -> bytes: ...


class _Unavailable:
    def resolve(self, tx: wire.WireTransaction) -> ResolvedMessage:
        raise WaitsSolders("раскрытие ALT не подключено (RpcMessageResolver) — ждёт solders")

    def build_v0(self, **kw) -> bytes:
        raise WaitsSolders("сборка MessageV0 не подключена (SoldersMessageBuilder) — ждёт solders")


UNAVAILABLE = _Unavailable()


# --- таблица ALT ----------------------------------------------------------------------------------------------------
def _content_hash(addrs: Sequence[str]) -> str:
    from .b58 import pubkey_bytes
    return hashlib.sha256(b"".join(pubkey_bytes(a) for a in addrs)).hexdigest()


def parse_alt(address: str, value: Mapping | None, slot: int) -> AltSnapshot:
    """Ответ getAccountInfo/getMultipleAccounts (encoding base64) → снимок таблицы. Любая неувязка — MessageError."""
    check_pubkey(address, "ALT")
    if value is None:
        raise MessageError("alt_missing", f"ALT {address}: счёта нет")
    if value.get("owner") != ALT_PROGRAM:
        raise MessageError("alt_owner", f"ALT {address}: владелец {value.get('owner')}, а не программа ALT")
    if value.get("executable"):
        raise MessageError("alt_layout", f"ALT {address}: исполняемый счёт")
    d = value.get("data")
    if not (isinstance(d, (list, tuple)) and len(d) == 2 and d[1] == "base64" and isinstance(d[0], str)):
        raise MessageError("alt_layout", f"ALT {address}: данные не base64")
    try:
        raw = base64.b64decode(d[0], validate=True)
    except ValueError:
        raise MessageError("alt_layout", f"ALT {address}: данные не base64") from None
    if len(raw) < ALT_META_SIZE or (len(raw) - ALT_META_SIZE) % 32:
        raise MessageError("alt_layout", f"ALT {address}: размер {len(raw)} не 56 + 32·n")
    typ, deact, last_ext, start = struct.unpack_from("<IQQB", raw, 0)
    if typ != ALT_TYPE_LOOKUP_TABLE:
        raise MessageError("alt_layout", f"ALT {address}: тип {typ} (не LookupTable)")
    tag = raw[21]
    if tag not in (0, 1):
        raise MessageError("alt_layout", f"ALT {address}: тег authority {tag}")
    authority = b58encode(raw[22:54]) if tag == 1 else None
    n = (len(raw) - ALT_META_SIZE) // 32
    if n > MAX_ALT_ADDRESSES:
        raise MessageError("alt_layout", f"ALT {address}: {n} адресов > {MAX_ALT_ADDRESSES}")
    addrs = tuple(b58encode(raw[ALT_META_SIZE + 32 * i:ALT_META_SIZE + 32 * (i + 1)]) for i in range(n))
    _cross_alt(address, raw, deact, last_ext, start, authority, addrs)
    return AltSnapshot(address=address, owner=ALT_PROGRAM, deactivation_slot=None if deact == U64_MAX else deact,
                       last_extended_slot=last_ext, authority=authority, addresses=addrs, slot=int(slot),
                       content_hash=_content_hash(addrs), last_extended_start=start)


def _cross_alt(address, raw, deact, last_ext, start, authority, addrs) -> None:
    """Второй разбор — solders (независимая реализация раскладки)."""
    from solders.address_lookup_table_account import AddressLookupTable
    try:
        t = AddressLookupTable.deserialize(raw)
    except Exception as e:     # noqa: BLE001 — любой отказ solders = таблица не читается
        raise MessageError("alt_layout", f"ALT {address}: solders не разобрал ({type(e).__name__})") from None
    m = t.meta
    same = (int(m.deactivation_slot) == deact and int(m.last_extended_slot) == last_ext
            and int(m.last_extended_slot_start_index) == start
            and (str(m.authority) if m.authority is not None else None) == authority
            and tuple(str(a) for a in t.addresses) == addrs)
    if not same:
        raise MessageError("alt_parsers_disagree", f"ALT {address}: разбор и solders расходятся")


def _fetch_values(rpc: Any, keys: list[str]) -> tuple[list, int]:
    """rpc.SolanaRpc (multiple_accounts) или rpc.RpcPool (read → первый здоровый узел)."""
    call = lambda ep: ep.multiple_accounts(keys, encoding="base64", commitment="confirmed")   # noqa: E731
    return rpc.read(call) if hasattr(rpc, "read") else call(rpc)


class AltCache:
    """Кэш снимков ALT. get() отдаёт снимки, в которых нужные индексы уже активны; иначе перечитывает таблицу.
    max_age_s — технический срок жизни записи (статус деактивации мог смениться), не денежный порог."""

    def __init__(self, rpc: Any, *, max_age_s: float = 30.0, clock: Callable[[], float] = time.monotonic):
        self.rpc, self.max_age_s, self.clock = rpc, max_age_s, clock
        self._lock = threading.Lock()
        self._rows: dict[str, tuple[AltSnapshot, float]] = {}
        self.fetches = 0

    def peek(self, address: str) -> AltSnapshot | None:
        with self._lock:
            row = self._rows.get(address)
        return row[0] if row else None

    def get(self, need: Mapping[str, int]) -> dict[str, AltSnapshot]:
        """need: адрес таблицы → максимальный нужный индекс (-1: нужна таблица целиком как есть)."""
        now = self.clock()
        out, stale = {}, []
        with self._lock:
            for addr, hi in need.items():
                row = self._rows.get(addr)
                if row and now - row[1] <= self.max_age_s and hi < row[0].active_len():
                    out[addr] = row[0]
                else:
                    stale.append(addr)
        for i in range(0, len(stale), 100):
            chunk = stale[i:i + 100]
            values, slot = _fetch_values(self.rpc, chunk)
            self.fetches += 1
            if not isinstance(values, list) or len(values) != len(chunk):
                raise MessageError("alt_rpc", "getMultipleAccounts: не тот размер ответа")
            for addr, v in zip(chunk, values):
                snap = parse_alt(addr, v, slot)
                with self._lock:
                    old = self._rows.get(addr)
                    if old and snap.addresses[:len(old[0].addresses)] != old[0].addresses:
                        self._rows.pop(addr, None)
                        raise MessageError("alt_content_changed",
                                           f"ALT {addr}: уже виденные адреса изменились (таблица только дописывается)")
                    if old and snap.slot < old[0].slot:        # узел отстал: старее, чем уже виденное
                        snap = old[0] if len(old[0].addresses) >= len(snap.addresses) else snap
                    self._rows[addr] = (snap, now)
                out[addr] = snap
        return out


# --- раскрытие ------------------------------------------------------------------------------------------------------
def cross_parse(m: wire.WireMessage) -> None:
    """Те же байты разбирает solders; всё, что видит проверка, обязано совпасть."""
    from solders.message import from_bytes_versioned
    try:
        sm = from_bytes_versioned(bytes(m.raw))
    except Exception as e:     # noqa: BLE001
        raise MessageError("solders_unparsed", f"solders не разобрал сообщение ({type(e).__name__})") from None
    h = sm.header
    ok = (h.num_required_signatures == m.num_required_signatures
          and h.num_readonly_signed_accounts == m.num_readonly_signed
          and h.num_readonly_unsigned_accounts == m.num_readonly_unsigned
          and tuple(str(k) for k in sm.account_keys) == m.static_keys
          and str(sm.recent_blockhash) == m.recent_blockhash
          and len(sm.instructions) == len(m.instructions))
    if ok:
        for a, b in zip(sm.instructions, m.instructions):
            if a.program_id_index != b.program_index or bytes(a.accounts) != bytes(b.accounts) or bytes(a.data) != b.data:
                ok = False
                break
    lookups = getattr(sm, "address_table_lookups", None) or []
    if ok and m.version == 0:
        ok = len(lookups) == len(m.lookups) and all(
            str(x.account_key) == y.table and bytes(x.writable_indexes) == bytes(y.writable)
            and bytes(x.readonly_indexes) == bytes(y.readonly) for x, y in zip(lookups, m.lookups))
    elif ok and lookups:
        ok = False
    if not ok:
        raise MessageError("parsers_disagree", "разбор провода и solders видят разные сообщения")


def resolve_with(m: wire.WireMessage, snaps: Mapping[str, AltSnapshot]) -> ResolvedMessage:
    """Полное сообщение по уже прочитанным таблицам (без сети)."""
    loaded_w: list[str] = []
    loaded_r: list[str] = []
    used: list[AltSnapshot] = []
    for lk in m.lookups:
        s = snaps.get(lk.table)
        if s is None:
            raise MessageError("alt_missing", f"ALT {lk.table}: не прочитана")
        if s.owner != ALT_PROGRAM:
            raise MessageError("alt_owner", f"ALT {lk.table}: чужой владелец")
        if s.deactivation_slot is not None:
            raise MessageError("alt_deactivated", f"ALT {lk.table}: деактивирована в слоте {s.deactivation_slot}")
        n = s.active_len()
        for i in (*lk.writable, *lk.readonly):
            if i >= n:
                raise MessageError("alt_index", f"ALT {lk.table}: индекс {i} вне активных {n}")
        loaded_w += [s.addresses[i] for i in lk.writable]
        loaded_r += [s.addresses[i] for i in lk.readonly]
        used.append(s)
    keys = tuple(m.static_keys) + tuple(loaded_w) + tuple(loaded_r)
    if len(set(keys)) != len(keys):
        raise MessageError("keys_duplicate", "ключ загружен дважды (статический и из ALT или из двух таблиц)")
    writable = {k for i, k in enumerate(m.static_keys) if m.static_writable(i)} | set(loaded_w)
    ixs = tuple(ResolvedInstruction(keys[ix.program_index], tuple(keys[a] for a in ix.accounts), bytes(ix.data))
                for ix in m.instructions)
    return ResolvedMessage(version=m.version, payer=m.fee_payer, signers=m.signers, writable=frozenset(writable),
                           readonly=frozenset(keys) - frozenset(writable), keys=keys, alts=tuple(used),
                           instructions=ixs, recent_blockhash=m.recent_blockhash, message_hash=m.message_hash,
                           n_static=len(m.static_keys))


class RpcMessageResolver:
    """MessageResolver по RPC: таблицы — через AltCache (свежие, активные, неизменные)."""

    def __init__(self, alts: AltCache):
        self.alts = alts

    def resolve(self, tx: wire.WireTransaction) -> ResolvedMessage:
        m = tx.message
        cross_parse(m)
        need: dict[str, int] = {}
        for lk in m.lookups:
            need[lk.table] = max([need.get(lk.table, -1), *lk.writable, *lk.readonly])
        return resolve_with(m, self.alts.get(need) if need else {})


# --- сборка ---------------------------------------------------------------------------------------------------------
def _metas(ix: Any) -> tuple[str, bytes, list[tuple[str, bool, bool]]]:
    """spot_router.Ix (program_id, accounts[.pubkey/.is_signer/.is_writable], data) или (pid, [(k, s, w)], data)."""
    if isinstance(ix, tuple):
        pid, accs, data = ix
        return pid, bytes(data), [tuple(a) for a in accs]
    return ix.program_id, bytes(ix.data), [(a.pubkey, a.is_signer, a.is_writable) for a in ix.accounts]


class SoldersMessageBuilder:
    """MessageV0 со своим плательщиком и blockhash. Таблицы — снимки из AltCache, не содержимое провайдера."""

    def build_v0(self, *, payer: str, instructions: Sequence[Any], recent_blockhash: str,
                 alts: Sequence[AltSnapshot]) -> bytes:
        from solders.address_lookup_table_account import AddressLookupTableAccount
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import MessageV0, to_bytes_versioned
        from solders.pubkey import Pubkey
        try:
            check_pubkey(payer, "плательщик")
            check_pubkey(recent_blockhash, "blockhash")
        except Base58Error as e:
            raise MessageError("build_input", str(e)) from None
        sx = []
        for ix in instructions:
            pid, data, metas = _metas(ix)
            sx.append(Instruction(Pubkey.from_string(pid), data,
                                  [AccountMeta(Pubkey.from_string(k), bool(s), bool(w)) for k, s, w in metas]))
        tables = []
        for s in alts:
            if s.deactivation_slot is not None:
                raise MessageError("alt_deactivated", f"ALT {s.address}: деактивирована")
            tables.append(AddressLookupTableAccount(Pubkey.from_string(s.address),
                                                    [Pubkey.from_string(a) for a in s.addresses[:s.active_len()]]))
        try:
            msg = MessageV0.try_compile(Pubkey.from_string(payer), sx, tables, Hash.from_string(recent_blockhash))
        except Exception as e:     # noqa: BLE001 — CompileError и прочее: собрать нельзя
            raise MessageError("build_failed", f"MessageV0 не собирается ({type(e).__name__})") from None
        out = to_bytes_versioned(msg)
        m = wire.parse_message(out)                      # обратный разбор своим строгим парсером
        if m.fee_payer != payer or m.recent_blockhash != recent_blockhash:
            raise MessageError("build_mismatch", "собранное сообщение: другой плательщик или blockhash")
        rm = resolve_with(m, {s.address: s for s in alts})
        want = [_metas(ix) for ix in instructions]
        if len(rm.instructions) != len(want) or any(
                (r.program_id, r.accounts, r.data) != (w[0], tuple(k for k, _, _ in w[2]), w[1])
                for r, w in zip(rm.instructions, want)):
            raise MessageError("build_mismatch", "собранное сообщение раскрывается не в те инструкции")
        for pid, _, metas in want:
            for k, s, w in metas:
                if s and k not in rm.signers:
                    raise MessageError("build_mismatch", "подписант инструкции не стал подписантом сообщения")
                if w and k not in rm.writable:
                    raise MessageError("build_mismatch", "writable-счёт инструкции стал только для чтения")
        return out
