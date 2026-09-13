"""Декодеры инструкций, которые могут стоять в транзакции свопа (ТЗ §9.1, SOLANA_ROUTERS §6, read_solana §2.1–2.2).

Верхний уровень (decode_top): ComputeBudget, System, Token, Token-2022, ATA, AddressLookupTable, Jupiter v6 и OKX
dex_solana — по on-chain IDL (borsh.IdlDecoder, sha256 закреплён). Неизвестная программа, неизвестный тег или
дискриминатор, неверная длина данных — DecodeError: кандидат непригоден, а не «пропускаем непонятное».
Вложенные (decode_inner): формат RPC innerInstructions — jsonParsed, частично разобранный (programId + счета) или
компилированный (индексы), Token/System приводятся к именам и полям jsonParsed.

Декодер только читает. Что разрешено — решает validate.py (манифест v1).
"""
from __future__ import annotations
import struct
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence
from . import (ALT_PROGRAM, ATA_PROGRAM, COMPUTE_BUDGET_PROGRAM, SYSTEM_PROGRAM, TOKEN_2022_PROGRAM, TOKEN_PROGRAM,
               TOKEN_PROGRAMS)
from .b58 import Base58Error, b58decode, b58encode
from .borsh import BorshError, IdlDecoder

JUPITER_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
OKX_ROUTER = "6m2CDdhRgxpH4WjvdzxAYbGxwdGUz5MziiL5jek2kBma"
# on-chain Anchor IDL, снятые 13.09 (sol_plan/probe_idl*.py); тот же sha256 закреплён в jupiter_spot/okx_sol_spot
JUPITER_IDL_SHA256 = "a31edf6096bcd4bb0292b84726722791548b47e41b85d2ed3a110c437e326dca"
OKX_IDL_SHA256 = "e76b4b28adbe69c01482b4d303a3e878dfee7bf1440c2e2683d20cf7f81e511b"
IDL_DIR = Path(__file__).with_name("idl")
LAMPORTS_PER_SIGNATURE = 5000
MAX_TX_COMPUTE_UNITS = 1_400_000
DEFAULT_CU_PER_IX = 200_000          # лимит по умолчанию без SetComputeUnitLimit (верхняя оценка: и для builtin)

CB_NAMES = {0: "RequestUnitsDeprecated", 1: "RequestHeapFrame", 2: "SetComputeUnitLimit", 3: "SetComputeUnitPrice",
            4: "SetLoadedAccountsDataSizeLimit"}
SYSTEM_NAMES = {0: "createAccount", 1: "assign", 2: "transfer", 3: "createAccountWithSeed", 4: "advanceNonce",
                5: "withdrawNonce", 6: "initializeNonce", 7: "authorizeNonce", 8: "allocate", 9: "allocateWithSeed",
                10: "assignWithSeed", 11: "transferWithSeed", 12: "upgradeNonce"}
TOKEN_NAMES = {0: "initializeMint", 1: "initializeAccount", 2: "initializeMultisig", 3: "transfer", 4: "approve",
               5: "revoke", 6: "setAuthority", 7: "mintTo", 8: "burn", 9: "closeAccount", 10: "freezeAccount",
               11: "thawAccount", 12: "transferChecked", 13: "approveChecked", 14: "mintToChecked", 15: "burnChecked",
               16: "initializeAccount2", 17: "syncNative", 18: "initializeAccount3", 19: "initializeMultisig2",
               20: "initializeMint2", 21: "getAccountDataSize", 22: "initializeImmutableOwner",
               23: "amountToUiAmount", 24: "uiAmountToAmount", 25: "initializeMintCloseAuthority",
               26: "transferFeeExtension", 27: "confidentialTransferExtension", 28: "defaultAccountStateExtension",
               29: "reallocate", 30: "memoTransferExtension", 31: "createNativeMint",
               32: "initializeNonTransferableMint", 33: "interestBearingMintExtension", 34: "cpiGuardExtension",
               35: "initializePermanentDelegate", 36: "transferHookExtension"}
ATA_NAMES = {0: "Create", 1: "CreateIdempotent", 2: "RecoverNested"}
AUTHORITY_TYPES = {0: "mintTokens", 1: "freezeAccount", 2: "accountOwner", 3: "closeAccount"}


class DecodeError(ValueError):
    """Инструкция не разбирается. code — короткая причина для журнала."""

    def __init__(self, code: str, text: str = ""):
        super().__init__(text or code)
        self.code = code


@dataclass(frozen=True)
class Decoded:
    program: str                         # имя программы: ComputeBudget | System | Token | Token2022 | ATA | Jupiter | OKX
    program_id: str
    name: str                            # имя инструкции (jsonParsed-стиль у нативных, IDL у Anchor)
    args: Mapping[str, Any]
    accounts: Mapping[str, str]          # имя счёта → адрес (по позициям IDL/спецификации)
    remaining: tuple[str, ...] = ()      # счета после фиксированных (маршрут AMM у роутеров)
    signer_roles: tuple[str, ...] = ()   # какие роли подписывают/распоряжаются (authority, owner, from…)


PROGRAM_NAMES = {COMPUTE_BUDGET_PROGRAM: "ComputeBudget", SYSTEM_PROGRAM: "System", TOKEN_PROGRAM: "Token",
                 TOKEN_2022_PROGRAM: "Token2022", ATA_PROGRAM: "ATA", ALT_PROGRAM: "AddressLookupTable",
                 JUPITER_PROGRAM: "Jupiter", OKX_ROUTER: "OKX"}


@lru_cache(maxsize=None)
def jupiter_idl() -> IdlDecoder:
    return IdlDecoder.load(IDL_DIR / "jupiter_v6.json", expect_sha256=JUPITER_IDL_SHA256)


@lru_cache(maxsize=None)
def okx_idl() -> IdlDecoder:
    return IdlDecoder.load(IDL_DIR / "okx_dex_solana.json", expect_sha256=OKX_IDL_SHA256)


def _need(accounts: Sequence[str], n: int, what: str) -> None:
    if len(accounts) < n:
        raise DecodeError("accounts_short", f"{what}: счетов {len(accounts)} < {n}")


def _named(names: Sequence[str], accounts: Sequence[str]) -> dict[str, str]:
    return {n: accounts[i] for i, n in enumerate(names) if i < len(accounts)}


# --- нативные программы ---------------------------------------------------------------------------------------------
def decode_compute_budget(data: bytes, accounts: Sequence[str]) -> Decoded:
    if not data:
        raise DecodeError("cb_empty")
    tag = data[0]
    name = CB_NAMES.get(tag)
    if name is None:
        raise DecodeError(f"cb_tag:{tag}")
    size = {2: 5, 3: 9, 4: 5, 1: 5}.get(tag)
    if size is not None and len(data) != size:
        raise DecodeError(f"cb_len:{tag}", f"{name}: {len(data)} байт вместо {size}")
    args: dict[str, Any] = {}
    if tag == 2:
        args["units"] = struct.unpack_from("<I", data, 1)[0]
    elif tag == 3:
        args["micro_lamports"] = struct.unpack_from("<Q", data, 1)[0]
    elif tag in (1, 4):
        args["bytes"] = struct.unpack_from("<I", data, 1)[0]
    return Decoded("ComputeBudget", COMPUTE_BUDGET_PROGRAM, name, args, {}, tuple(accounts))


def decode_system(data: bytes, accounts: Sequence[str]) -> Decoded:
    if len(data) < 4:
        raise DecodeError("system_short")
    tag = struct.unpack_from("<I", data, 0)[0]
    name = SYSTEM_NAMES.get(tag)
    if name is None:
        raise DecodeError(f"system_tag:{tag}")
    a, args, acc, roles = accounts, {}, {}, ()
    try:
        if tag == 2:                                       # Transfer {lamports}
            if len(data) != 12:
                raise DecodeError("system_len:transfer")
            _need(a, 2, name)
            args = {"lamports": struct.unpack_from("<Q", data, 4)[0]}
            acc, roles = {"source": a[0], "destination": a[1]}, ("source",)
        elif tag == 0:                                     # CreateAccount {lamports, space, owner}
            if len(data) != 52:
                raise DecodeError("system_len:createAccount")
            _need(a, 2, name)
            lam, space = struct.unpack_from("<QQ", data, 4)
            args = {"lamports": lam, "space": space, "owner": b58encode(data[20:52])}
            acc, roles = {"source": a[0], "newAccount": a[1]}, ("source", "newAccount")
        elif tag == 3:                                     # CreateAccountWithSeed {base, seed, lamports, space, owner}
            base = b58encode(data[4:36])
            n = struct.unpack_from("<Q", data, 36)[0]
            if len(data) != 44 + n + 48:
                raise DecodeError("system_len:createAccountWithSeed")
            seed = data[44:44 + n].decode("utf-8", "replace")
            lam, space = struct.unpack_from("<QQ", data, 44 + n)
            args = {"base": base, "seed": seed, "lamports": lam, "space": space,
                    "owner": b58encode(data[60 + n:92 + n])}
            _need(a, 2, name)
            acc, roles = {"source": a[0], "newAccount": a[1]}, ("source",)
        elif tag == 11:                                    # TransferWithSeed {lamports, seed, owner}
            _need(a, 3, name)
            args = {"lamports": struct.unpack_from("<Q", data, 4)[0]}
            acc, roles = {"source": a[0], "sourceBase": a[1], "destination": a[2]}, ("sourceBase",)
        elif tag in (1, 8):                                # Assign {owner} / Allocate {space}
            _need(a, 1, name)
            acc, roles = {"account": a[0]}, ("account",)
        elif tag == 4:                                     # AdvanceNonceAccount
            _need(a, 3, name)
            acc, roles = {"nonceAccount": a[0], "nonceAuthority": a[2]}, ("nonceAuthority",)
        else:
            acc = _named(("account",), a)
    except struct.error:
        raise DecodeError(f"system_len:{name}") from None
    return Decoded("System", SYSTEM_PROGRAM, name, args, acc, (), roles)


def decode_token(program_id: str, data: bytes, accounts: Sequence[str]) -> Decoded:
    prog = PROGRAM_NAMES[program_id]
    if not data:
        raise DecodeError("token_empty")
    tag = data[0]
    name = TOKEN_NAMES.get(tag)
    if name is None:
        raise DecodeError(f"token_tag:{tag}")
    a, args, acc, roles = accounts, {}, {}, ()
    try:
        if tag == 3:                                        # Transfer {amount}: source, destination, authority
            _need(a, 3, name)
            if len(data) != 9:
                raise DecodeError("token_len:transfer")
            args = {"amount": struct.unpack_from("<Q", data, 1)[0]}
            acc, roles = {"source": a[0], "destination": a[1], "authority": a[2]}, ("authority",)
        elif tag == 12:                                     # TransferChecked {amount, decimals}
            _need(a, 4, name)
            if len(data) != 10:
                raise DecodeError("token_len:transferChecked")
            args = {"amount": struct.unpack_from("<Q", data, 1)[0], "decimals": data[9]}
            acc, roles = {"source": a[0], "mint": a[1], "destination": a[2], "authority": a[3]}, ("authority",)
        elif tag in (4, 13):                                # Approve(Checked): source, [mint], delegate, owner
            k = 4 if tag == 13 else 3
            _need(a, k, name)
            args = {"amount": struct.unpack_from("<Q", data, 1)[0]}
            acc = ({"source": a[0], "mint": a[1], "delegate": a[2], "owner": a[3]} if tag == 13 else
                   {"source": a[0], "delegate": a[1], "owner": a[2]})
            roles = ("owner",)
        elif tag == 5:                                      # Revoke: source, owner
            _need(a, 2, name)
            acc, roles = {"source": a[0], "owner": a[1]}, ("owner",)
        elif tag == 6:                                      # SetAuthority {type, COption<new>}: account, current
            _need(a, 2, name)
            if len(data) < 3:
                raise DecodeError("token_len:setAuthority")
            new = b58encode(data[3:35]) if data[2] == 1 and len(data) >= 35 else None
            args = {"authorityType": AUTHORITY_TYPES.get(data[1], str(data[1])), "newAuthority": new}
            acc, roles = {"account": a[0], "authority": a[1]}, ("authority",)
        elif tag == 9:                                      # CloseAccount: account, destination, owner
            _need(a, 3, name)
            acc, roles = {"account": a[0], "destination": a[1], "owner": a[2]}, ("owner",)
        elif tag in (8, 15):                                # Burn(Checked): account, mint, authority
            _need(a, 3, name)
            args = {"amount": struct.unpack_from("<Q", data, 1)[0]}
            acc, roles = {"account": a[0], "mint": a[1], "authority": a[2]}, ("authority",)
        elif tag in (10, 11):                               # Freeze/Thaw: account, mint, authority
            _need(a, 3, name)
            acc, roles = {"account": a[0], "mint": a[1], "authority": a[2]}, ("authority",)
        elif tag in (7, 14):                                # MintTo(Checked): mint, account, authority
            _need(a, 3, name)
            acc, roles = {"mint": a[0], "account": a[1], "authority": a[2]}, ("authority",)
        elif tag == 17:                                     # SyncNative: account
            _need(a, 1, name)
            if len(data) != 1:
                raise DecodeError("token_len:syncNative")
            acc = {"account": a[0]}
        elif tag == 18:                                     # InitializeAccount3 {owner}: account, mint
            _need(a, 2, name)
            if len(data) != 33:
                raise DecodeError("token_len:initializeAccount3")
            args = {"owner": b58encode(data[1:33])}
            acc = {"account": a[0], "mint": a[1]}
        elif tag == 22:                                     # InitializeImmutableOwner: account
            _need(a, 1, name)
            acc = {"account": a[0]}
        elif tag == 21:                                     # GetAccountDataSize: mint
            _need(a, 1, name)
            acc = {"mint": a[0]}
        else:
            acc = _named(("account",), a)
    except struct.error:
        raise DecodeError(f"token_len:{name}") from None
    return Decoded(prog, program_id, name, args, acc, (), roles)


def decode_ata(data: bytes, accounts: Sequence[str]) -> Decoded:
    tag = 0 if not data else data[0]
    if len(data) > 1 or tag not in ATA_NAMES:
        raise DecodeError(f"ata_tag:{tag}")
    name = ATA_NAMES[tag]
    if tag == 2:
        _need(accounts, 7, name)
        acc = _named(("nestedAccount", "nestedMint", "destination", "ownerAccount", "ownerMint", "wallet",
                      "tokenProgram"), accounts)
        return Decoded("ATA", ATA_PROGRAM, name, {}, acc, tuple(accounts[7:]), ("wallet",))
    _need(accounts, 6, name)
    acc = _named(("payer", "account", "wallet", "mint", "systemProgram", "tokenProgram"), accounts)
    return Decoded("ATA", ATA_PROGRAM, name, {}, acc, tuple(accounts[6:]), ("payer",))


def decode_anchor(program_id: str, dec: IdlDecoder, data: bytes, accounts: Sequence[str]) -> Decoded:
    try:
        name, args = dec.decode_ix(data)
    except BorshError as e:
        d = bytes(data[:8]).hex()
        code = "idl_unknown_ix" if dec.name_of(data) is None else "idl_args"
        raise DecodeError(f"{code}:{d}", str(e)) from None
    names = [a["name"] for a in dec.accounts(name)]
    _need(accounts, len(names), name)
    return Decoded(PROGRAM_NAMES[program_id], program_id, name, args, _named(names, accounts),
                   tuple(accounts[len(names):]))


def decode_top(program_id: str, data: bytes, accounts: Sequence[str]) -> Decoded:
    """Инструкция верхнего уровня полного сообщения (счета — адреса после раскрытия ALT)."""
    data = bytes(data)
    if program_id == COMPUTE_BUDGET_PROGRAM:
        return decode_compute_budget(data, accounts)
    if program_id == SYSTEM_PROGRAM:
        return decode_system(data, accounts)
    if program_id in TOKEN_PROGRAMS:
        return decode_token(program_id, data, accounts)
    if program_id == ATA_PROGRAM:
        return decode_ata(data, accounts)
    if program_id == JUPITER_PROGRAM:
        return decode_anchor(program_id, jupiter_idl(), data, accounts)
    if program_id == OKX_ROUTER:
        return decode_anchor(program_id, okx_idl(), data, accounts)
    if program_id == ALT_PROGRAM:
        return Decoded("AddressLookupTable", ALT_PROGRAM, f"tag:{data[:4].hex()}", {}, {}, tuple(accounts))
    raise DecodeError("program_unknown", f"программа {program_id} не в манифесте")


# --- вложенные инструкции из simulateTransaction / getTransaction ------------------------------------------------
@dataclass(frozen=True)
class InnerIx:
    program_id: str
    name: str | None                     # None — программа не Token/System или не разобрана
    info: Mapping[str, Any] = field(default_factory=dict)
    accounts: tuple[str, ...] = ()
    stack_height: int | None = None
    error: str | None = None             # не удалось разобрать форму


_PARSED_PROGRAMS = {"spl-token": TOKEN_PROGRAM, "spl-token-2022": TOKEN_2022_PROGRAM, "system": SYSTEM_PROGRAM}


def _native_info(d: Decoded) -> dict[str, Any]:
    info = dict(d.accounts)
    info.update(d.args)
    return info


def decode_inner(raw: Mapping, keys: Sequence[str] | None) -> InnerIx:
    """Одна вложенная инструкция в любой из трёх форм RPC. keys — полный список ключей сообщения (для индексов)."""
    sh = raw.get("stackHeight") if isinstance(raw, Mapping) else None
    if not isinstance(raw, Mapping):
        return InnerIx("?", None, error="not_object")
    if "parsed" in raw:                                   # jsonParsed
        pid = raw.get("programId") or _PARSED_PROGRAMS.get(raw.get("program"), "?")
        p = raw.get("parsed")
        if isinstance(p, Mapping) and isinstance(p.get("info"), Mapping):
            info = dict(p["info"])
            accs = tuple(v for v in info.values() if isinstance(v, str))
            return InnerIx(pid, str(p.get("type")), info, accs, sh)
        return InnerIx(pid, None, {"parsed": p}, (), sh)
    try:
        if "programIdIndex" in raw:                       # компилированная форма: индексы в ключи сообщения
            if keys is None:
                return InnerIx("?", None, error="compiled_without_keys", stack_height=sh)
            pid = keys[raw["programIdIndex"]]
            accs = tuple(keys[i] for i in raw.get("accounts") or [])
        else:                                             # частично разобранная: адреса как есть
            pid = raw.get("programId")
            accs = tuple(raw.get("accounts") or [])
        data = b58decode(raw.get("data") or "")
    except (IndexError, TypeError, Base58Error):
        return InnerIx(str(raw.get("programId", "?")), None, error="unresolved", stack_height=sh)
    if not isinstance(pid, str):
        return InnerIx("?", None, error="no_program", stack_height=sh)
    if pid in TOKEN_PROGRAMS or pid == SYSTEM_PROGRAM:
        try:
            d = decode_token(pid, data, accs) if pid in TOKEN_PROGRAMS else decode_system(data, accs)
        except DecodeError as e:
            return InnerIx(pid, None, {}, accs, sh, error=e.code)
        return InnerIx(pid, d.name, _native_info(d), accs, sh)
    return InnerIx(pid, None, {}, accs, sh)


def pda(seeds: Sequence[bytes], program_id: str) -> str:
    from .accounts import find_program_address
    return find_program_address(seeds, program_id)[0]
