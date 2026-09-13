"""Подпись Solana: контракт «подписываем только закреплённое» (ТЗ §9.1, CONNECTION §2.2).

Подписант — любой объект протокола SolanaSigner: keys.SolanaKey (поток profiles, Ed25519 pycryptodome, секрет —
из keypair.py) или SeedSigner — обвязка над внешним бэкендом (фабрика seed → public_key_bytes()/sign()):
сверяет публичный ключ бэкенда с секретом и каждую подпись проверяет независимо (ed25519.verify). Бэкенд solders —
solders_backend (Keypair.from_seed; сам Keypair наружу не выходит: его str() — это весь секрет в base58);
solders_signer(secret) — SeedSigner поверх него. Фабрики нет — WaitsSolders. Своей подписи на чистом Python здесь
нет: арифметика не константного времени, секрет утёк бы по времени.

sign_validated — подписать только байты message, чей sha256 закреплён после проверки (message_hash): изменились —
отказ. Первая версия — один подписант = наш кошелёк и плательщик; внешние подписанты (Jupiter Order RFQ/gasless)
— отказ, пока нет проверенного восстановления по requestId (G14, external_signer_recovery=false).
sign_checked — то же плюс условия подписи целиком: статическая проверка (validate.py) и эффекты симуляции прошли
для ЭТИХ байтов тем же манифестом; blockhash сообщения — ровно тот, чей lastValidBlockHeight получен в том же
ответе getLatestBlockhash (G08), и до истечения осталось не меньше заданного владельцем числа высот.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable
from . import WaitsSolders, ed25519, wire
from .b58 import Base58Error, b58encode, pubkey_bytes
from .keypair import SolanaSecret


class SignError(RuntimeError):
    """Подписывать нельзя: сообщение изменилось, чужой или лишний подписант, подпись не проверяется."""


@runtime_checkable
class SolanaSigner(Protocol):
    def public_key(self) -> str: ...

    def sign_message(self, message: bytes) -> bytes: ...     # 64 байта Ed25519


class SignBackend(Protocol):
    def public_key_bytes(self) -> bytes: ...

    def sign(self, message: bytes) -> bytes: ...


BackendFactory = Callable[[bytes], SignBackend]           # seed(32) → бэкенд (solders: Keypair.from_seed)


class _NoCopy:
    __slots__ = ()

    def __reduce__(self):
        raise TypeError("ключ не сериализуется")

    def __reduce_ex__(self, protocol):
        raise TypeError("ключ не сериализуется")

    def __copy__(self):
        raise TypeError("ключ не копируется")

    def __deepcopy__(self, memo):
        raise TypeError("ключ не копируется")


class _SoldersBackend(_NoCopy):
    """Keypair solders за стеной: наружу — только публичный ключ и подпись."""
    __slots__ = ("_kp",)

    def __init__(self, seed: bytes):
        from solders.keypair import Keypair
        if len(seed) != 32:
            raise SignError("seed Ed25519: нужно 32 байта")
        self._kp = Keypair.from_seed(bytes(seed))

    def public_key_bytes(self) -> bytes:
        return bytes(self._kp.pubkey())

    def sign(self, message: bytes) -> bytes:
        return bytes(self._kp.sign_message(bytes(message)))

    def __repr__(self) -> str:
        return "<solders-backend>"

    __str__ = __repr__


def solders_backend(seed: bytes) -> SignBackend:
    return _SoldersBackend(seed)


class SeedSigner(_NoCopy):
    __slots__ = ("_backend", "_pub")

    def __init__(self, secret: SolanaSecret, factory: BackendFactory | None = None):
        if factory is None:
            raise WaitsSolders("бэкенд подписи Ed25519 не установлен (solders.keypair.Keypair.from_seed)")
        backend = secret.with_seed(factory)
        pub = pubkey_bytes(secret.public_key())
        if bytes(backend.public_key_bytes()) != pub:
            raise SignError("бэкенд подписи дал другой публичный ключ, чем секрет")
        self._backend, self._pub = backend, pub

    def public_key(self) -> str:
        return b58encode(self._pub)

    def sign_message(self, message: bytes) -> bytes:
        sig = bytes(self._backend.sign(bytes(message)))
        if len(sig) != 64 or not ed25519.verify(self._pub, bytes(message), sig):
            raise SignError("подпись бэкенда не проходит проверку Ed25519")
        return sig

    def __repr__(self) -> str:
        return f"<solana-signer {self.public_key()}>"

    __str__ = __repr__


def solders_signer(secret: SolanaSecret) -> SeedSigner:
    """Подписант кошелька из проверенного секрета (keypair.parse_secret_b58 / load_keypair_file) на solders."""
    return SeedSigner(secret, solders_backend)


def sign_validated(signer: SolanaSigner, message_bytes: bytes, pinned_hash: str) -> bytes:
    """Закреплённое сообщение → подписанные байты транзакции (формат провода). Любая неувязка — до подписи."""
    message_bytes = bytes(message_bytes)
    if wire.message_hash(message_bytes) != pinned_hash:
        raise SignError("сообщение изменилось после проверки — не подписываю")
    msg = wire.parse_message(message_bytes)
    me = signer.public_key()
    try:
        me_b = pubkey_bytes(me)
    except Base58Error:
        raise SignError("публичный ключ подписанта — не адрес Solana") from None
    if msg.num_required_signatures != 1:
        raise SignError(f"подписантов {msg.num_required_signatures}: внешние подписи не поддержаны "
                        "(external_signer_recovery=false)")
    if msg.fee_payer != me:
        raise SignError(f"плательщик {msg.fee_payer}, а ключ подписанта {me}")
    sig = bytes(signer.sign_message(message_bytes))
    if len(sig) != 64 or not ed25519.verify(me_b, message_bytes, sig):
        raise SignError("подпись не проходит проверку Ed25519 — в журнал не пишу")
    return wire.assemble([sig], message_bytes)


# --- срок по точному blockhash и полный набор условий подписи -------------------------------------------------------
@dataclass(frozen=True)
class BlockhashRef:
    """Пара из ОДНОГО ответа getLatestBlockhash: hash и его lastValidBlockHeight (G08)."""
    blockhash: str
    last_valid_block_height: int
    exact: bool = True           # False — высота не из того же ответа (OKX /swap и т.п.): подписывать нельзя


def expiry_reasons(message_bytes: bytes, ref: BlockhashRef | None, *, block_height: int | None,
                   min_validity_heights: int | None) -> tuple[str, ...]:
    """Причины не подписывать по сроку. Высота — getBlockHeight того же уровня commitment, что и blockhash."""
    r = []
    bh = wire.parse_message(bytes(message_bytes)).recent_blockhash
    if ref is None:
        return ("blockhash_unknown",)
    if bh != ref.blockhash:
        r.append("blockhash_mismatch")
    if not ref.exact:
        r.append("lvbh_not_exact")
    if min_validity_heights is None:
        r.append("limit_missing:min_blockhash_validity_heights")
    if block_height is None:
        r.append("block_height_unknown")
    elif block_height > ref.last_valid_block_height:
        r.append("blockhash_expired")
    elif min_validity_heights is not None and ref.last_valid_block_height - block_height < min_validity_heights:
        r.append("blockhash_expiring")
    return tuple(r)


def sign_checked(signer: SolanaSigner, message_bytes: bytes, validation, simulation, *, blockhash: BlockhashRef,
                 block_height: int | None, min_validity_heights: int | None) -> bytes:
    """Подпись только при полном «да»: validation и simulation — validate.Validation для ЭТИХ байтов (ok, без
    причин, тот же manifest_version), срок blockhash достаточен. Иначе SignError с причинами, подпись не создаётся."""
    message_bytes = bytes(message_bytes)
    mh = wire.message_hash(message_bytes)
    why: list[str] = []
    for tag, v in (("validation", validation), ("simulation", simulation)):
        if v is None:
            why.append(f"{tag}_missing")
        elif not v.ok or v.reasons:
            why.append(f"{tag}_failed")
        elif v.message_hash != mh:
            why.append(f"{tag}_other_message")
        elif v.manifest_version is None:
            why.append(f"{tag}_manifest_unknown")
    if validation is not None and simulation is not None and validation.manifest_version != simulation.manifest_version:
        why.append("manifest_mismatch")
    why += expiry_reasons(message_bytes, blockhash, block_height=block_height,
                          min_validity_heights=min_validity_heights)
    if why:
        raise SignError("не подписываю: " + ", ".join(dict.fromkeys(why)))
    return sign_validated(signer, message_bytes, mh)
