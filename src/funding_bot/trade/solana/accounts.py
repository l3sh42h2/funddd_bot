"""Mint и token-счета Solana: Token и Token-2022, allowlist расширений, PDA/ATA, rent (ТЗ §2.2, S03–S08).

Источник — RPC, не провайдер котировок. Mint читается дважды: jsonParsed (удобно) и сырые байты (base64) с
собственным разбором раскладки и TLV Token-2022; расхождение — отказ (разборщик RPC не единственная истина).
Политика расширений — явный allowlist: mint — metadataPointer, tokenMetadata (как у ANSEM), token-счёт —
immutableOwner (его ставит ATA-программа Token-2022). TransferFee, TransferHook, PermanentDelegate,
DefaultAccountState, NonTransferable, конфиденциальные, InterestBearing/ScaledUiAmount, Pausable и НЕИЗВЕСТНЫЕ —
отказ нового входа с названной причиной, а не «налог 0» (S04, S05). Token-2022 целиком не запрещён (S03).
Freeze/mint authority у mint — заметка, не отказ (у USDC freeze authority есть); их смена против
замороженного инструмента — отказ через identity_diff (S06).

ATA выводится по программе КОНКРЕТНОГО mint: seeds [owner, token_program, mint], программа ATA, PDA = sha256 вне
кривой Ed25519 (S07). Размер счёта и rent — по фактической программе и расширениям, rent — у узла.
Адреса — base58 как есть; сравнение побайтное, .lower() нет (S01, S02).
"""
from __future__ import annotations
import base64, binascii, dataclasses, hashlib, json, struct
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from . import ATA_PROGRAM, NATIVE_MINT, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, TOKEN_PROGRAMS
from .b58 import b58encode, check_pubkey, pubkey_bytes
from .ed25519 import is_on_curve

MINT_EXT_ALLOW = frozenset({"metadataPointer", "tokenMetadata"})
ACCOUNT_EXT_ALLOW = frozenset({"immutableOwner"})
MINT_LEN = 82
ACCOUNT_LEN = 165
ACCOUNT_TYPE_MINT, ACCOUNT_TYPE_ACCOUNT = 1, 2
# ExtensionType Token-2022 (u16 в TLV) → имя как в jsonParsed
EXT_TYPES = {
    1: "transferFeeConfig", 2: "transferFeeAmount", 3: "mintCloseAuthority", 4: "confidentialTransferMint",
    5: "confidentialTransferAccount", 6: "defaultAccountState", 7: "immutableOwner", 8: "memoTransfer",
    9: "nonTransferable", 10: "interestBearingConfig", 11: "cpiGuard", 12: "permanentDelegate",
    13: "nonTransferableAccount", 14: "transferHook", 15: "transferHookAccount",
    16: "confidentialTransferFeeConfig", 17: "confidentialTransferFeeAmount", 18: "metadataPointer",
    19: "tokenMetadata", 20: "groupPointer", 21: "tokenGroup", 22: "groupMemberPointer", 23: "tokenGroupMember",
    24: "confidentialMintBurn", 25: "scaledUiAmountConfig", 26: "pausableConfig", 27: "pausableAccount"}
_WHY = {
    "transferFeeConfig": "TransferFee: комиссия за перевод, нетто меньше брутто — не поддержано",
    "transferFeeAmount": "TransferFee на счёте — не поддержано",
    "transferHook": "TransferHook: чужая программа на каждом переводе — не поддержано",
    "transferHookAccount": "TransferHook на счёте — не поддержано",
    "permanentDelegate": "PermanentDelegate: постоянный делегат может списать токены",
    "defaultAccountState": "DefaultAccountState: счета могут создаваться замороженными",
    "nonTransferable": "NonTransferable: токен нельзя переводить",
    "nonTransferableAccount": "NonTransferable на счёте",
    "confidentialTransferMint": "конфиденциальные переводы — не поддержано",
    "confidentialTransferAccount": "конфиденциальные переводы на счёте — не поддержано",
    "confidentialTransferFeeConfig": "конфиденциальные комиссии — не поддержано",
    "confidentialTransferFeeAmount": "конфиденциальные комиссии на счёте — не поддержано",
    "confidentialMintBurn": "конфиденциальная эмиссия — не поддержано",
    "interestBearingConfig": "InterestBearing: отображаемая сумма ≠ raw — не поддержано",
    "scaledUiAmountConfig": "ScaledUiAmount: отображаемая сумма ≠ raw — не поддержано",
    "pausableConfig": "Pausable: эмитент может остановить переводы",
    "pausableAccount": "Pausable на счёте",
    "mintCloseAuthority": "MintCloseAuthority: mint можно закрыть и пересоздать",
    "memoTransfer": "MemoTransfer: входящие переводы без memo отвергаются",
    "cpiGuard": "CpiGuard: переводы через CPI роутера запрещены",
    "unparseableExtension": "расширение не разобрано RPC",
}
PDA_MARKER = b"ProgramDerivedAddress"


class AccountError(ValueError):
    """Счёт не того вида или не разбирается — решать по нему нельзя."""


def _why(name: str) -> str:
    return _WHY.get(name, f"расширение {name}: не поддержано до отдельной реализации")


def _digits(x: Any, what: str) -> int:
    """Сырое количество из jsonParsed: строка цифр. float/пусто — ошибка (не 0)."""
    if isinstance(x, str) and x.isdigit():
        return int(x)
    raise AccountError(f"{what}: не строка цифр ({type(x).__name__})")


def _uint(x: Any, what: str) -> int:
    if isinstance(x, bool) or not isinstance(x, int) or x < 0:
        raise AccountError(f"{what}: не целое ≥ 0")
    return x


def _opt_key(x: Any, what: str) -> str | None:
    return None if x is None else check_pubkey(x, what)


def _ext_list(raw: Any, what: str) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise AccountError(f"{what}: extensions не список")
    out = []
    for e in raw:
        if not isinstance(e, dict) or not isinstance(e.get("extension"), str):
            raise AccountError(f"{what}: расширение не разобрано")
        out.append((e["extension"], json.dumps(e.get("state"), sort_keys=True, separators=(",", ":"))))
    return tuple(out)


# --- mint ----------------------------------------------------------------------------------------
@dataclass(frozen=True)
class MintInfo:
    address: str
    program: str
    decimals: int
    supply: int
    is_initialized: bool
    mint_authority: str | None
    freeze_authority: str | None
    extensions: tuple[tuple[str, str], ...]        # (имя, состояние JSON с сортировкой ключей)
    space: int
    lamports: int
    slot: int
    raw_diff: tuple[str, ...] = ()                 # расхождения jsonParsed и сырых байтов (read_mint)

    @property
    def ext_names(self) -> tuple[str, ...]:
        return tuple(n for n, _ in self.extensions)

    def ext_state(self, name: str) -> Any:
        for n, s in self.extensions:
            if n == name:
                return json.loads(s)
        return None


def parse_mint(address: str, value: Mapping | None, slot: int) -> MintInfo:
    """getAccountInfo(jsonParsed).value → MintInfo. Не mint, не Token/Token-2022, не разобран — AccountError."""
    check_pubkey(address, "mint")
    if value is None:
        raise AccountError(f"mint {address}: счёта нет")
    owner = value.get("owner")
    if owner not in TOKEN_PROGRAMS:
        raise AccountError(f"mint {address}: владелец {owner} — не Token и не Token-2022")
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "mint" or not isinstance(parsed.get("info"), dict):
        raise AccountError(f"mint {address}: RPC не разобрал счёт как mint")
    info = parsed["info"]
    dec = _uint(info.get("decimals"), "decimals")
    if dec > 255:
        raise AccountError("decimals > 255")
    exts = _ext_list(info.get("extensions"), f"mint {address}")
    if owner == TOKEN_PROGRAM and exts:
        raise AccountError(f"mint {address}: у классического Token расширений не бывает")
    if not isinstance(info.get("isInitialized"), bool):
        raise AccountError("isInitialized не bool")
    return MintInfo(address=address, program=owner, decimals=dec, supply=_digits(info.get("supply"), "supply"),
                    is_initialized=info["isInitialized"],
                    mint_authority=_opt_key(info.get("mintAuthority"), "mintAuthority"),
                    freeze_authority=_opt_key(info.get("freezeAuthority"), "freezeAuthority"),
                    extensions=exts, space=_uint(value.get("space", data.get("space")), "space"),
                    lamports=_uint(value.get("lamports"), "lamports"), slot=_uint(slot, "slot"))


def _coption_key(b: bytes, off: int) -> str | None:
    """COption<Pubkey>: тег u32 (0 — нет, 1 — есть) + 32 байта. При None тело может хранить старый ключ —
    Token его не зануляет, поэтому смотрим только тег."""
    tag = struct.unpack_from("<I", b, off)[0]
    if tag == 0:
        return None
    if tag == 1:
        return b58encode(b[off + 4:off + 36])
    raise AccountError(f"COption: тег {tag}")


def _tlv(buf: bytes) -> tuple[tuple[int, bytes], ...]:
    """TLV расширений Token-2022: тип u16, длина u16, значение. Тип 0 (Uninitialized) — дальше пусто."""
    out, off = [], 0
    while off < len(buf):
        if off + 2 > len(buf):
            raise AccountError("TLV: обрыв на типе")
        t = struct.unpack_from("<H", buf, off)[0]
        if t == 0:
            break
        if off + 4 > len(buf):
            raise AccountError("TLV: обрыв на длине")
        ln = struct.unpack_from("<H", buf, off + 2)[0]
        if off + 4 + ln > len(buf):
            raise AccountError("TLV: обрыв на значении")
        out.append((t, bytes(buf[off + 4:off + 4 + ln])))
        off += 4 + ln
    return tuple(out)


def _ext_tail(data: bytes, program: str, base_len: int, acc_type: int, what: str) -> tuple[tuple[int, bytes], ...]:
    if program == TOKEN_PROGRAM:
        if len(data) != base_len:
            raise AccountError(f"{what}: классический Token, а длина {len(data)} ≠ {base_len}")
        return ()
    if len(data) == base_len:
        return ()
    if len(data) <= ACCOUNT_LEN:
        raise AccountError(f"{what}: длина {len(data)} — ни базовая, ни с расширениями")
    if data[ACCOUNT_LEN] != acc_type:
        raise AccountError(f"{what}: тип счёта Token-2022 {data[ACCOUNT_LEN]} ≠ {acc_type}")
    return _tlv(data[ACCOUNT_LEN + 1:])


def parse_mint_raw(data: bytes, program: str) -> dict:
    """Сырые байты mint (раскладка SPL Token, 82 байта; Token-2022 — + заполнение до 165, тип 1, TLV)."""
    if program not in TOKEN_PROGRAMS:
        raise AccountError(f"программа {program} — не Token/Token-2022")
    if len(data) < MINT_LEN:
        raise AccountError(f"mint: {len(data)} байт < {MINT_LEN}")
    init = data[45]
    if init not in (0, 1):
        raise AccountError("mint: is_initialized не 0/1")
    return {"mint_authority": _coption_key(data, 0), "supply": struct.unpack_from("<Q", data, 36)[0],
            "decimals": data[44], "is_initialized": bool(init), "freeze_authority": _coption_key(data, 46),
            "extensions": _ext_tail(bytes(data), program, MINT_LEN, ACCOUNT_TYPE_MINT, "mint")}


def _ext_raw_names(exts: Sequence[tuple[int, bytes]]) -> list[str]:
    return [EXT_TYPES.get(t, f"unknown#{t}") for t, _ in exts]


def _metadata_pointer_raw(exts: Sequence[tuple[int, bytes]]) -> dict | None:
    """MetadataPointer: authority(32, нули = нет) + metadata_address(32)."""
    for t, v in exts:
        if t == 18:
            if len(v) != 64:
                raise AccountError("metadataPointer: не 64 байта")
            auth = None if not any(v[:32]) else b58encode(v[:32])
            addr = None if not any(v[32:]) else b58encode(v[32:])
            return {"authority": auth, "metadataAddress": addr}
    return None


def raw_diff_mint(info: MintInfo, raw: Mapping) -> tuple[str, ...]:
    diffs = []
    for k in ("decimals", "is_initialized", "mint_authority", "freeze_authority"):
        if getattr(info, k) != raw[k]:
            diffs.append(f"{k}: jsonParsed {getattr(info, k)} ≠ байты {raw[k]}")
    names = _ext_raw_names(raw["extensions"])
    if sorted(names) != sorted(info.ext_names):
        diffs.append(f"расширения: jsonParsed {sorted(info.ext_names)} ≠ байты {sorted(names)}")
    mp_raw, mp = _metadata_pointer_raw(raw["extensions"]), info.ext_state("metadataPointer")
    if mp_raw is not None and isinstance(mp, dict) and (mp.get("authority"), mp.get("metadataAddress")) != \
            (mp_raw["authority"], mp_raw["metadataAddress"]):
        diffs.append("metadataPointer: jsonParsed ≠ байты")
    return tuple(diffs)


def account_bytes(value: Mapping) -> bytes:
    """getAccountInfo(base64).value → данные счёта."""
    data = value.get("data") if isinstance(value, Mapping) else None
    if not (isinstance(data, list) and len(data) == 2 and data[1] == "base64" and isinstance(data[0], str)):
        raise AccountError("данные счёта не в base64")
    try:
        return base64.b64decode(data[0], validate=True)
    except (binascii.Error, ValueError):
        raise AccountError("данные счёта: не base64") from None


def read_mint(rpc, mint: str, *, commitment: str = "confirmed") -> MintInfo:
    """Mint по RPC: jsonParsed + сырые байты, расхождения в raw_diff (mint_policy их не пропустит)."""
    value, slot = rpc.account_info(mint, encoding="jsonParsed", commitment=commitment)
    info = parse_mint(mint, value, slot)
    vb, _ = rpc.account_info(mint, encoding="base64", commitment=commitment)
    if vb is None:
        return dataclasses.replace(info, raw_diff=("сырые байты: счёта нет",))
    diffs = []
    if vb.get("owner") != info.program:
        diffs.append(f"владелец: jsonParsed {info.program} ≠ base64 {vb.get('owner')}")
        return dataclasses.replace(info, raw_diff=tuple(diffs))
    raw = parse_mint_raw(account_bytes(vb), info.program)
    return dataclasses.replace(info, raw_diff=tuple(diffs) + raw_diff_mint(info, raw))


def mint_policy(info: MintInfo, allow: frozenset = MINT_EXT_ALLOW) -> list[str]:
    """Причины отказа нового входа (пусто — схема допустима; остальные проверки — отдельно)."""
    why = []
    if info.program not in TOKEN_PROGRAMS:
        why.append(f"программа mint {info.program} — не Token/Token-2022")
    if not info.is_initialized:
        why.append("mint не инициализирован")
    for name in info.ext_names:
        if name not in allow:
            why.append(_why(name))
    why.extend(f"jsonParsed и сырые байты расходятся: {d}" for d in info.raw_diff)
    return why


def mint_notes(info: MintInfo) -> list[str]:
    """Справка для doctor/capability: не отказ."""
    notes = []
    if info.freeze_authority:
        notes.append(f"freeze authority {info.freeze_authority}: эмитент может заморозить счёт")
    if info.mint_authority:
        notes.append(f"mint authority {info.mint_authority}: эмиссия не закрыта")
    mp, tm = info.ext_state("metadataPointer"), info.ext_state("tokenMetadata")
    if isinstance(mp, dict) and mp.get("authority"):
        notes.append(f"metadataPointer изменяем (authority {mp['authority']})")
    if isinstance(tm, dict) and tm.get("updateAuthority"):
        notes.append(f"метаданные изменяемы (updateAuthority {tm['updateAuthority']})")
    return notes


def mint_identity(info: MintInfo) -> dict:
    """Поля, которые замораживаются в инструменте: изменение любого — новая версия, старый план недействителен."""
    mp, tm = info.ext_state("metadataPointer"), info.ext_state("tokenMetadata")
    return {"mint": info.address, "program": info.program, "decimals": info.decimals,
            "mint_authority": info.mint_authority, "freeze_authority": info.freeze_authority,
            "extensions": sorted(info.ext_names),
            "metadata_pointer_authority": mp.get("authority") if isinstance(mp, dict) else None,
            "metadata_update_authority": tm.get("updateAuthority") if isinstance(tm, dict) else None}


def identity_diff(expected: Mapping[str, Any], info: MintInfo) -> list[str]:
    """Сверка замороженной идентичности с текущим mint (S06). Сравнение точное: регистр адреса значим."""
    cur = mint_identity(info)
    out = []
    for k, v in expected.items():
        if k not in cur:
            out.append(f"{k}: поле не известно сверке")
        elif (list(v) if isinstance(v, (list, tuple)) else v) != cur[k]:
            out.append(f"{k}: было {v!r}, стало {cur[k]!r}")
    for k in ("mint", "program", "decimals"):
        if k not in expected:
            out.append(f"{k}: не заморожено в инструменте")
    return out


# --- token-счёт ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class TokenAccountInfo:
    address: str
    mint: str
    owner: str
    program: str
    amount: int
    decimals: int
    state: str                                   # initialized | frozen | uninitialized
    delegate: str | None
    delegated_amount: int
    close_authority: str | None
    is_native: bool
    extensions: tuple[tuple[str, str], ...]
    space: int
    lamports: int
    slot: int
    raw_diff: tuple[str, ...] = ()

    @property
    def ext_names(self) -> tuple[str, ...]:
        return tuple(n for n, _ in self.extensions)


def parse_token_account(address: str, value: Mapping | None, slot: int) -> TokenAccountInfo:
    check_pubkey(address, "token-счёт")
    if value is None:
        raise AccountError(f"счёт {address}: нет")
    owner_prog = value.get("owner")
    if owner_prog not in TOKEN_PROGRAMS:
        raise AccountError(f"счёт {address}: программа {owner_prog} — не Token/Token-2022")
    data = value.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "account" or not isinstance(parsed.get("info"), dict):
        raise AccountError(f"счёт {address}: RPC не разобрал как token-счёт")
    info = parsed["info"]
    ta = info.get("tokenAmount")
    if not isinstance(ta, dict):
        raise AccountError("tokenAmount нет")
    delegated = info.get("delegatedAmount")
    exts = _ext_list(info.get("extensions"), f"счёт {address}")
    if owner_prog == TOKEN_PROGRAM and exts:
        raise AccountError(f"счёт {address}: у классического Token расширений не бывает")
    if not isinstance(info.get("isNative"), bool):
        raise AccountError("isNative не bool")
    return TokenAccountInfo(
        address=address, mint=check_pubkey(info.get("mint"), "mint счёта"),
        owner=check_pubkey(info.get("owner"), "владелец счёта"), program=owner_prog,
        amount=_digits(ta.get("amount"), "amount"), decimals=_uint(ta.get("decimals"), "decimals"),
        state=str(info.get("state")), delegate=_opt_key(info.get("delegate"), "delegate"),
        delegated_amount=_digits(delegated.get("amount"), "delegatedAmount") if isinstance(delegated, dict) else 0,
        close_authority=_opt_key(info.get("closeAuthority"), "closeAuthority"), is_native=info["isNative"],
        extensions=exts, space=_uint(value.get("space", data.get("space")), "space"),
        lamports=_uint(value.get("lamports"), "lamports"), slot=_uint(slot, "slot"))


_STATES = {0: "uninitialized", 1: "initialized", 2: "frozen"}


def parse_token_account_raw(data: bytes, program: str) -> dict:
    """Сырые байты token-счёта: mint, owner, amount, delegate, state, is_native, delegated, close_authority."""
    if program not in TOKEN_PROGRAMS:
        raise AccountError(f"программа {program} — не Token/Token-2022")
    data = bytes(data)
    if len(data) < ACCOUNT_LEN:
        raise AccountError(f"token-счёт: {len(data)} байт < {ACCOUNT_LEN}")
    st = data[108]
    if st not in _STATES:
        raise AccountError(f"token-счёт: состояние {st}")
    native_tag = struct.unpack_from("<I", data, 109)[0]
    if native_tag not in (0, 1):
        raise AccountError("token-счёт: тег is_native")
    return {"mint": b58encode(data[0:32]), "owner": b58encode(data[32:64]),
            "amount": struct.unpack_from("<Q", data, 64)[0], "delegate": _coption_key(data, 72),
            "state": _STATES[st], "is_native": native_tag == 1,
            "delegated_amount": struct.unpack_from("<Q", data, 121)[0], "close_authority": _coption_key(data, 129),
            "extensions": _ext_tail(data, program, ACCOUNT_LEN, ACCOUNT_TYPE_ACCOUNT, "token-счёт")}


def raw_diff_account(info: TokenAccountInfo, raw: Mapping) -> tuple[str, ...]:
    diffs = [f"{k}: jsonParsed {getattr(info, k)} ≠ байты {raw[k]}"
             for k in ("mint", "owner", "delegate", "state", "is_native", "close_authority")
             if getattr(info, k) != raw[k]]
    names = _ext_raw_names(raw["extensions"])
    if sorted(names) != sorted(info.ext_names):
        diffs.append(f"расширения: jsonParsed {sorted(info.ext_names)} ≠ байты {sorted(names)}")
    return tuple(diffs)


def read_token_account(rpc, address: str, *, commitment: str = "confirmed") -> TokenAccountInfo | None:
    """None — счёта нет (его ещё не создали). Сбой RPC — исключение, а не None."""
    value, slot = rpc.account_info(address, encoding="jsonParsed", commitment=commitment)
    if value is None:
        return None
    info = parse_token_account(address, value, slot)
    vb, _ = rpc.account_info(address, encoding="base64", commitment=commitment)
    if vb is None or vb.get("owner") != info.program:
        return dataclasses.replace(info, raw_diff=("сырые байты: счёт исчез или сменил программу",))
    raw = parse_token_account_raw(account_bytes(vb), info.program)
    # amount не сверяем: между двумя чтениями он мог измениться — это не идентичность счёта
    return dataclasses.replace(info, raw_diff=raw_diff_account(info, raw))


def account_policy(info: TokenAccountInfo, *, owner: str, mint: str, program: str,
                   allow: frozenset = ACCOUNT_EXT_ALLOW, require_ata: bool = True) -> list[str]:
    """Причины, по которым счёт НЕ годится как наш счёт сделки (S08). Чужой счёт не трогаем и не «чиним»."""
    why = []
    if info.program != program:
        why.append(f"счёт под программой {info.program}, а mint — под {program}")
    if info.mint != mint:
        why.append(f"mint счёта {info.mint} ≠ {mint}")
    if info.owner != owner:
        why.append(f"владелец счёта {info.owner} ≠ {owner}")
    if require_ata and program in TOKEN_PROGRAMS and info.address != ata(owner, mint, program):
        why.append("адрес не ATA (владелец, mint, программа mint)")
    if info.state == "frozen":
        why.append("счёт заморожен")
    elif info.state != "initialized":
        why.append(f"состояние счёта {info.state}")
    if info.delegate:
        why.append(f"делегат {info.delegate} может списывать с счёта")
    if info.close_authority:
        why.append(f"close authority {info.close_authority}")
    if info.is_native and mint != NATIVE_MINT:
        why.append("isNative у не-wSOL счёта")
    for name in info.ext_names:
        if name not in allow:
            why.append(_why(name))
    why.extend(f"jsonParsed и сырые байты расходятся: {d}" for d in info.raw_diff)
    return why


# --- PDA / ATA / rent ----------------------------------------------------------------------------
def create_program_address(seeds: Sequence[bytes], program_id: str) -> str | None:
    """sha256(seeds ‖ program ‖ "ProgramDerivedAddress"); точка на кривой — None (такого PDA нет)."""
    if len(seeds) > 16 or any(len(s) > 32 for s in seeds):
        raise ValueError("seeds: не больше 16 по 32 байта")
    h = hashlib.sha256(b"".join(bytes(s) for s in seeds) + pubkey_bytes(program_id) + PDA_MARKER).digest()
    return None if is_on_curve(h) else b58encode(h)


def find_program_address(seeds: Sequence[bytes], program_id: str) -> tuple[str, int]:
    for bump in range(255, -1, -1):
        a = create_program_address([*seeds, bytes([bump])], program_id)
        if a is not None:
            return a, bump
    raise ValueError("PDA не найден ни для одного bump")


def ata(owner: str, mint: str, token_program: str) -> str:
    """ATA владельца для mint под программой ЭТОГО mint (Token или Token-2022)."""
    if token_program not in TOKEN_PROGRAMS:
        raise ValueError(f"программа {token_program} — не Token/Token-2022")
    return find_program_address([pubkey_bytes(owner), pubkey_bytes(token_program), pubkey_bytes(mint)],
                                ATA_PROGRAM)[0]


def ata_space(program: str, mint_ext_names: Sequence[str] = ()) -> int:
    """Размер ATA: Token — 165; Token-2022 — 165 + тип счёта (1) + TLV ImmutableOwner (4). Расширения mint вне
    allowlist требуют своих расширений счёта — размер для них не считаем (вход по ним и так запрещён)."""
    if program == TOKEN_PROGRAM:
        return ACCOUNT_LEN
    if program == TOKEN_2022_PROGRAM:
        extra = sorted(set(mint_ext_names) - MINT_EXT_ALLOW)
        if extra:
            raise ValueError(f"размер счёта для расширений {extra} не поддержан")
        return ACCOUNT_LEN + 1 + 4
    raise ValueError(f"программа {program} — не Token/Token-2022")


def ata_rent_lamports(rpc, program: str, mint_ext_names: Sequence[str] = ()) -> int:
    """Возвратный депозит за создание ATA — у узла по фактическому размеру (не константой)."""
    return rpc.rent_exempt_lamports(ata_space(program, mint_ext_names))
