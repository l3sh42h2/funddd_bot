"""Base58 (алфавит Bitcoin/Solana) на чистом Python: адреса, подписи, секреты.

Строго: символ вне алфавита — ошибка, а не пропуск; адрес — ровно 32 байта, подпись — 64. Регистр значим:
'A' и 'a' — разные цифры, поэтому .lower()/.upper() адреса Solana запрещены (S01, S02).
Тексты ошибок не содержат саму строку: через этот модуль идут и секреты кошелька.
"""
from __future__ import annotations

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}


class Base58Error(ValueError):
    """Не base58 или не того размера. Значение в текст не попадает."""


def b58decode(s: str) -> bytes:
    if not isinstance(s, str):
        raise Base58Error(f"base58: ожидалась строка, а не {type(s).__name__}")
    n = 0
    for pos, c in enumerate(s):
        d = _INDEX.get(c)
        if d is None:
            raise Base58Error(f"base58: недопустимый символ в позиции {pos} (длина {len(s)})")
        n = n * 58 + d
    zeros = len(s) - len(s.lstrip("1"))              # ведущие '1' — нулевые байты
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * zeros + body


def b58encode(b: bytes) -> str:
    b = bytes(b)
    n = int.from_bytes(b, "big")
    out = []
    while n:
        n, r = divmod(n, 58)
        out.append(ALPHABET[r])
    zeros = len(b) - len(b.lstrip(b"\0"))
    return "1" * zeros + "".join(reversed(out))


def decode_exact(s: str, size: int, what: str) -> bytes:
    b = b58decode(s)
    if len(b) != size:
        raise Base58Error(f"{what}: {len(b)} байт вместо {size}")
    return b


def pubkey_bytes(s: str) -> bytes:
    """Адрес Solana → 32 байта. Пустой, короткий, с пробелом по краям — ошибка (strip() не делаем)."""
    return decode_exact(s, 32, "адрес Solana")


def is_pubkey(s) -> bool:
    try:
        pubkey_bytes(s)
        return True
    except Base58Error:
        return False


def pubkey_str(b: bytes) -> str:
    if len(b) != 32:
        raise Base58Error(f"адрес Solana: {len(b)} байт вместо 32")
    return b58encode(b)


def signature_bytes(s: str) -> bytes:
    return decode_exact(s, 64, "подпись Solana")


def check_pubkey(s: str, what: str = "адрес") -> str:
    """Проверить и вернуть ту же строку (канонический base58 как есть — ровно то, что пришло)."""
    try:
        pubkey_bytes(s)
    except Base58Error as e:
        raise Base58Error(f"{what}: {e}") from None
    return s
