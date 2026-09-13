"""Ed25519 на чистом Python (RFC 8032) — только то, что работает с ПУБЛИЧНЫМИ данными, плюс вывод публичного
ключа из seed для сверки секрета при загрузке.

is_on_curve — ровно как Pubkey::is_on_curve в Solana (curve25519-dalek CompressedEdwardsY::decompress().is_some()):
знаковый бит не влияет, y берётся из младших 255 бит без требования канонического представления (dalek приводит
по модулю p). На этом стоит вывод PDA/ATA: адрес PDA — хеш, который НЕ является точкой кривой.
verify — проверка подписи (RFC 8032 §5.1.7, без кофактора): сверка журнала «подписанные байты подписаны нашим
кошельком по нашему сообщению». Подписи здесь НЕТ: арифметика не константного времени, секретный скаляр в ней
утёк бы по времени — подпись ждёт solders. public_from_seed считает скаляр один раз на старте (сверка секрета
с его публичной половиной) — это осознанное исключение.
"""
from __future__ import annotations
import hashlib

P = 2 ** 255 - 19
L = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQRT_M1 = pow(2, (P - 1) // 4, P)
_MASK255 = (1 << 255) - 1


def _inv(x: int) -> int:
    return pow(x, P - 2, P)


def _recover_x(y: int, sign: int) -> int | None:
    """x по y и знаку (строго по RFC 8032: y < p, x = 0 со знаком 1 — отказ)."""
    if y >= P:
        return None
    x2 = (y * y - 1) * _inv(D * y * y + 1) % P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQRT_M1 % P
    if (x * x - x2) % P:
        return None
    if (x & 1) != sign:
        x = P - x
    return x


def is_on_curve(b: bytes) -> bool:
    """32 байта — сжатая точка кривой? Семантика dalek (см. модуль): значение y по модулю p, знак не важен."""
    if len(b) != 32:
        raise ValueError(f"точка: {len(b)} байт вместо 32")
    y = (int.from_bytes(b, "little") & _MASK255) % P
    u = (y * y - 1) % P
    v = (D * y * y + 1) % P                 # d — не квадрат, поэтому v ≠ 0 при любом y
    x2 = u * _inv(v) % P
    return x2 == 0 or pow(x2, (P - 1) // 2, P) == 1


# --- арифметика точек в расширенных координатах (X, Y, Z, T), x = X/Z, y = Y/Z, xy = T/Z ---
_BY = 4 * _inv(5) % P
_BX = _recover_x(_BY, 0)
_B = (_BX, _BY, 1, _BX * _BY % P)
_ZERO = (0, 1, 1, 0)


def _add(p1, p2):
    x1, y1, z1, t1 = p1
    x2, y2, z2, t2 = p2
    a = (y1 - x1) * (y2 - x2) % P
    b = (y1 + x1) * (y2 + x2) % P
    c = 2 * t1 * t2 * D % P
    d = 2 * z1 * z2 % P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % P, g * h % P, f * g % P, e * h % P)


def _mul(s: int, pt):
    q = _ZERO
    while s > 0:
        if s & 1:
            q = _add(q, pt)
        pt = _add(pt, pt)
        s >>= 1
    return q


def _eq(p1, p2) -> bool:
    x1, y1, z1, _ = p1
    x2, y2, z2, _ = p2
    return (x1 * z2 - x2 * z1) % P == 0 and (y1 * z2 - y2 * z1) % P == 0


def _compress(pt) -> bytes:
    x, y, z, _ = pt
    zi = _inv(z)
    x, y = x * zi % P, y * zi % P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(b: bytes):
    if len(b) != 32:
        return None
    n = int.from_bytes(b, "little")
    y, sign = n & _MASK255, n >> 255
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % P)


def public_from_seed(seed: bytes) -> bytes:
    """Публичный ключ из 32-байтного seed (RFC 8032 §5.1.5). Только для сверки секрета при загрузке."""
    if len(seed) != 32:
        raise ValueError(f"seed: {len(seed)} байт вместо 32")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _compress(_mul(a, _B))


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Подпись Ed25519 верна? Любой мусор (не точка, S ≥ L, не та длина) — False, без исключений."""
    if len(public) != 32 or len(signature) != 64:
        return False
    a_pt = _decompress(public)
    r_pt = _decompress(signature[:32])
    if a_pt is None or r_pt is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= L:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(), "little") % L
    return _eq(_mul(s, _B), _add(r_pt, _mul(k, a_pt)))
