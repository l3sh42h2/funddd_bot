"""Структурный разбор байтов транзакции Solana (legacy и v0) и явная кодировка payload (ТЗ §9.1, G13).

Кодировку не угадываем: Jupiter отдаёт транзакцию base64, OKX `/swap` — tx.data base58, данные инструкций —
base64; вызывающий называет её явно (encoding), и строка другой кодировки отвергается, а не «пробуется иначе».
Разбор строгий: compact-u16 только канонический, индексы в границах, программа — статический ключ (v0),
ни одного лишнего байта в конце; версии кроме legacy и 0 — отказ (v1 в сети не активна, ТЗ SOLANA_ROUTERS §6).

Это НЕ валидатор: раскрытие ALT, декодеры программ и манифест — validate.py/message.py (ждут solders). Здесь —
то, что журналу нужно из подписанных байтов: первая подпись (txid), blockhash, плательщик, подписанты и хеш
сообщения (sha256 байтов message), по которому закрепляется проверенное сообщение.
"""
from __future__ import annotations
import base64, binascii, hashlib
from dataclasses import dataclass
from typing import Literal
from . import ed25519
from .b58 import Base58Error, b58decode, b58encode

Encoding = Literal["base64", "base58"]
PayloadKind = Literal["tx", "message", "instruction_data"]
SIG_LEN = 64
ZERO_SIG = b"\0" * SIG_LEN                  # пустой слот подписи: подписант ещё не подписал


class WireError(ValueError):
    """Байты — не транзакция/сообщение поддержанного формата."""


def compact_u16(buf: bytes, off: int) -> tuple[int, int]:
    """short_vec Solana: 1–3 байта, 7 бит на байт, старший — продолжение. Неканоническая запись — ошибка."""
    val = 0
    for i in range(3):
        if off + i >= len(buf):
            raise WireError("compact-u16: обрыв")
        b = buf[off + i]
        if i == 2 and b > 0x03:
            raise WireError("compact-u16: больше 16 бит")
        val |= (b & 0x7F) << (7 * i)
        if not b & 0x80:
            if i > 0 and b == 0:
                raise WireError("compact-u16: неканоническая запись")
            return val, off + i + 1
    raise WireError("compact-u16: больше 3 байт")


def encode_compact_u16(n: int) -> bytes:
    if not 0 <= n <= 0xFFFF:
        raise WireError(f"compact-u16: {n} вне 0..65535")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


@dataclass(frozen=True)
class WireInstruction:
    program_index: int
    accounts: tuple[int, ...]
    data: bytes


@dataclass(frozen=True)
class WireLookup:
    table: str
    writable: tuple[int, ...]
    readonly: tuple[int, ...]


@dataclass(frozen=True)
class WireMessage:
    version: str | int                      # "legacy" | 0
    num_required_signatures: int
    num_readonly_signed: int
    num_readonly_unsigned: int
    static_keys: tuple[str, ...]
    recent_blockhash: str
    instructions: tuple[WireInstruction, ...]
    lookups: tuple[WireLookup, ...]
    raw: bytes

    @property
    def fee_payer(self) -> str:
        return self.static_keys[0]

    @property
    def signers(self) -> tuple[str, ...]:
        return self.static_keys[:self.num_required_signatures]

    @property
    def message_hash(self) -> str:
        return message_hash(self.raw)

    @property
    def n_loaded(self) -> int:
        return sum(len(lk.writable) + len(lk.readonly) for lk in self.lookups)

    def static_writable(self, i: int) -> bool:
        """Запись в i-й статический ключ (правило заголовка). Ключи из ALT здесь не раскрыты — см. message.py."""
        n, s = len(self.static_keys), self.num_required_signatures
        if not 0 <= i < n:
            raise IndexError(i)
        if i < s:
            return i < s - self.num_readonly_signed
        return i < n - self.num_readonly_unsigned


@dataclass(frozen=True)
class WireTransaction:
    signatures: tuple[bytes, ...]
    message: WireMessage
    raw: bytes

    @property
    def signature(self) -> str:
        """Первая подпись = txid, известна ДО отправки (для полностью подписанной транзакции)."""
        return b58encode(self.signatures[0])

    @property
    def fully_signed(self) -> bool:
        return all(s != ZERO_SIG for s in self.signatures)

    def signature_ok(self) -> tuple[bool, ...]:
        """Каждая подпись верна для своего подписанта по байтам message (пустой слот — False)."""
        m = self.message
        return tuple(sig != ZERO_SIG and ed25519.verify(b58decode(m.static_keys[i]), m.raw, sig)
                     for i, sig in enumerate(self.signatures))


def message_hash(message_bytes: bytes) -> str:
    return hashlib.sha256(message_bytes).hexdigest()


def _key(buf: bytes, off: int) -> str:
    if off + 32 > len(buf):
        raise WireError("обрыв на ключе")
    return b58encode(buf[off:off + 32])


def _idx_list(buf: bytes, off: int) -> tuple[tuple[int, ...], int]:
    n, off = compact_u16(buf, off)
    if off + n > len(buf):
        raise WireError("обрыв на списке индексов")
    return tuple(buf[off:off + n]), off + n


def parse_message(b: bytes) -> WireMessage:
    b = bytes(b)
    if len(b) < 4:
        raise WireError("сообщение короче заголовка")
    off = 0
    version: str | int = "legacy"
    if b[0] & 0x80:
        version = b[0] & 0x7F
        if version != 0:
            raise WireError(f"версия сообщения {version} не поддержана (только legacy и 0)")
        off = 1
    nsig, nro_s, nro_u = b[off], b[off + 1], b[off + 2]
    off += 3
    nkeys, off = compact_u16(b, off)
    keys = []
    for _ in range(nkeys):
        keys.append(_key(b, off))
        off += 32
    if len(set(keys)) != len(keys):
        raise WireError("повтор статического ключа")
    blockhash = _key(b, off)
    off += 32
    nix, off = compact_u16(b, off)
    ixs = []
    for _ in range(nix):
        if off >= len(b):
            raise WireError("обрыв на инструкции")
        prog = b[off]
        off += 1
        accs, off = _idx_list(b, off)
        ndata, off = compact_u16(b, off)
        if off + ndata > len(b):
            raise WireError("обрыв на данных инструкции")
        ixs.append(WireInstruction(prog, accs, b[off:off + ndata]))
        off += ndata
    lookups = []
    if version == 0:
        nlk, off = compact_u16(b, off)
        for _ in range(nlk):
            table = _key(b, off)
            off += 32
            w, off = _idx_list(b, off)
            r, off = _idx_list(b, off)
            if not w and not r:
                raise WireError("ALT без индексов")
            lookups.append(WireLookup(table, w, r))
    if off != len(b):
        raise WireError(f"лишние {len(b) - off} байт после сообщения")
    msg = WireMessage(version, nsig, nro_s, nro_u, tuple(keys), blockhash, tuple(ixs), tuple(lookups), b)
    # правила заголовка (sanitize Solana): плательщик пишет и подписывает, индексы в границах
    if nsig < 1 or nsig > nkeys:
        raise WireError("подписантов 0 или больше, чем ключей")
    if nro_s >= nsig:
        raise WireError("плательщик помечен только для чтения")
    if nro_u > nkeys - nsig:
        raise WireError("readonly-unsigned больше, чем неподписантов")
    total = nkeys + msg.n_loaded
    if total > 256:
        raise WireError("больше 256 ключей")
    for ix in ixs:
        if ix.program_index == 0 or ix.program_index >= nkeys:
            raise WireError("программа — плательщик или вне статических ключей")
        if any(a >= total for a in ix.accounts):
            raise WireError("индекс счёта вне ключей")
    return msg


def parse_transaction(b: bytes) -> WireTransaction:
    b = bytes(b)
    nsig, off = compact_u16(b, 0)
    if nsig == 0:
        raise WireError("транзакция без подписей")
    if off + nsig * SIG_LEN > len(b):
        raise WireError("обрыв на подписях")
    sigs = tuple(b[off + i * SIG_LEN:off + (i + 1) * SIG_LEN] for i in range(nsig))
    msg = parse_message(b[off + nsig * SIG_LEN:])
    if nsig != msg.num_required_signatures:
        raise WireError(f"подписей {nsig}, а заголовок требует {msg.num_required_signatures}")
    return WireTransaction(sigs, msg, b)


def decode_payload(raw: str, *, encoding: Encoding) -> bytes:
    """Строка → байты строго по названной кодировке. Неканоническая запись, пробелы, urlsafe — отказ."""
    if not isinstance(raw, str) or not raw:
        raise WireError("payload: пустой или не строка")
    if encoding == "base64":
        try:
            out = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise WireError("payload: не base64") from None
        if base64.b64encode(out).decode() != raw:
            raise WireError("payload: неканонический base64")
        return out
    if encoding == "base58":
        try:
            return b58decode(raw)
        except Base58Error:
            raise WireError("payload: не base58") from None
    raise WireError(f"payload: неизвестная кодировка {encoding!r}")


def decode(raw: str, *, kind: PayloadKind, encoding: Encoding):
    """payload_kind + encoding → разобранный объект: tx → WireTransaction, message → WireMessage,
    instruction_data → bytes. Кодировку и вид задаёт адаптер провайдера по контракту, не по содержимому."""
    data = decode_payload(raw, encoding=encoding)
    if kind == "tx":
        return parse_transaction(data)
    if kind == "message":
        return parse_message(data)
    if kind == "instruction_data":
        return data
    raise WireError(f"payload: неизвестный вид {kind!r}")


def assemble(signatures: list[bytes] | tuple[bytes, ...], message_bytes: bytes) -> bytes:
    """Подписи + байты message → байты транзакции (формат провода). Число подписей сверяется с заголовком."""
    msg = parse_message(message_bytes)
    if len(signatures) != msg.num_required_signatures or any(len(s) != SIG_LEN for s in signatures):
        raise WireError("число или длина подписей не совпадает с заголовком")
    return encode_compact_u16(len(signatures)) + b"".join(signatures) + bytes(message_bytes)
