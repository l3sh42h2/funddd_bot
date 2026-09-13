"""Секрет кошелька Solana: разбор и сверка (CONNECTION §2.2; у владельца — строка base58 из 64 байт).

Только разбор уже прочитанного значения — окружение читает keys.py (единственное место с ключами):
  parse_secret_b58  — base58 от 64 байт: seed(32) + публичный ключ(32) (SOLANA_SECRET_B58);
  load_keypair_file — JSON-массив из 64 целых 0..255 (формат solana-keygen, SOLANA_KEYPAIR_FILE), права 0600.
Публичная половина обязана совпасть с ключом, выведенным из seed (иначе секрет повреждён или склеен из двух).
Hex-строка (ключ EVM/HL) или 32-байтный seed отвергаются с понятной причиной (S19: секрет не той роли).

Значение секрета не попадает ни в текст ошибки, ни в repr, ни в pickle/copy. Сырой seed отдаётся только
бэкенду подписи (sign.SeedSigner) через with_seed().
"""
from __future__ import annotations
import json, os, re, stat
from pathlib import Path
from typing import Callable, TypeVar
from . import ed25519
from .b58 import Base58Error, b58decode, b58encode

_HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
T = TypeVar("T")


class SolanaKeyError(RuntimeError):
    """Отказ разбора секрета. В тексте — имя источника и публичные адреса, никогда не значение."""


class SolanaSecret:
    """Проверенный секрет: seed + публичный ключ. Печатается как <solana-key АДРЕС>, не копируется, не
    сериализуется. Подписать сам не умеет — это sign.SeedSigner с бэкендом Ed25519."""
    __slots__ = ("_seed", "_pub", "source")

    def __init__(self, seed: bytes, pub: bytes, source: str):
        self._seed = bytes(seed)
        self._pub = bytes(pub)
        self.source = source

    def public_key(self) -> str:
        return b58encode(self._pub)

    def with_seed(self, fn: Callable[[bytes], T]) -> T:
        """Отдать seed функции (фабрике бэкенда подписи) и вернуть её результат."""
        return fn(self._seed)

    def __repr__(self) -> str:
        return f"<solana-key {self.public_key()}>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return repr(self)

    def __reduce__(self):
        raise TypeError("ключ не сериализуется")

    def __reduce_ex__(self, protocol):
        raise TypeError("ключ не сериализуется")

    def __copy__(self):
        raise TypeError("ключ не копируется")

    def __deepcopy__(self, memo):
        raise TypeError("ключ не копируется")


def _from_64(b: bytes, source: str) -> SolanaSecret:
    if len(b) == 32:
        raise SolanaKeyError(f"{source}: 32 байта — seed без публичной половины; нужен 64-байтный секрет")
    if len(b) != 64:
        raise SolanaKeyError(f"{source}: {len(b)} байт вместо 64")
    seed, pub = bytes(b[:32]), bytes(b[32:])
    if ed25519.public_from_seed(seed) != pub:
        raise SolanaKeyError(f"{source}: публичная половина не выводится из секретной — секрет повреждён или склеен")
    return SolanaSecret(seed, pub, source)


def parse_secret_b58(raw: str, source: str = "SOLANA_SECRET_B58") -> SolanaSecret:
    if not isinstance(raw, str) or not raw.strip():
        raise SolanaKeyError(f"{source}: пусто")
    s = raw.strip()
    if _HEX_RE.match(s):
        raise SolanaKeyError(f"{source}: 64 hex-символа — это ключ EVM/HL, а не секрет Solana (base58)")
    if s.startswith("["):
        raise SolanaKeyError(f"{source}: JSON-массив — это keypair-файл, его путь кладётся в SOLANA_KEYPAIR_FILE")
    try:
        b = b58decode(s)
    except Base58Error:
        raise SolanaKeyError(f"{source}: не base58 (длина {len(s)})") from None
    return _from_64(b, source)


def load_keypair_file(path: str | os.PathLike, *, check_perms: bool = True,
                      source: str = "SOLANA_KEYPAIR_FILE") -> SolanaSecret:
    p = Path(path)
    try:
        st = p.stat()
    except OSError as e:
        raise SolanaKeyError(f"{source}: файл недоступен ({type(e).__name__})") from None
    if not stat.S_ISREG(st.st_mode):
        raise SolanaKeyError(f"{source}: не обычный файл")
    if check_perms and st.st_mode & 0o077:
        raise SolanaKeyError(f"{source}: права {oct(st.st_mode & 0o777)} — доступ группе/прочим, нужен 0600")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SolanaKeyError(f"{source}: не читается как JSON") from None
    if not isinstance(data, list) or any(type(x) is not int or not 0 <= x <= 255 for x in data):
        raise SolanaKeyError(f"{source}: не массив байтов 0..255")
    return _from_64(bytes(data), source)
