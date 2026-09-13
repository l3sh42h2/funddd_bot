"""Разбор чека getTransaction (encoding=json, maxSupportedTransactionVersion=0) — ТЗ §9.3, SOLANA_ROUTERS §7,
S14–S18.

Токены: только pre/postTokenBalances с сырым `amount` (не uiAmount), по accountIndex в полном списке ключей
(статические + loadedAddresses.writable + readonly). Счёт сделки — (owner, mint, программа mint); созданный в
транзакции счёт считается от 0, закрытый — до 0. Кошелёк целиком не заменяет сделку: наши счета того же mint вне
списка сделки, изменившиеся в транзакции, — аномалия, а не «добавим к сделке» (S15). Чужой получатель выхода
(маршрут отдал токены другому владельцу) — наш выход 0.
Нет meta, нет owner/programId у нужного mint, нет loadedAddresses у v0 — ReceiptIncomplete: суммы НЕИЗВЕСТНЫ,
хеджировать по котировке нельзя (UNKNOWN_AMOUNT), это не ноль.

SOL: meta.fee — вся плата (base + priority), платит fee payer; tip — отдельный перевод, в fee не входит (S17).
Rent: депозит в созданные НАШИ token-счета и возврат из закрытых — отдельно от торговли (S18); wSOL-токены
наших счетов — торговый SOL, не rent. external_lamports — сколько SOL ушло из «домена» кошелька (кошелёк + его
token-счета, обёрнутый wSOL внутри) сверх fee: tip, rent чужих счетов, SOL, отданный в своп; для пары без SOL
всё это должно объясняться tip и rent.
Неуспешная транзакция (meta.err): fee удержана, токены не двигались — иначе разбору не верим.
"""
from __future__ import annotations
import base64, binascii, struct
from dataclasses import dataclass
from typing import Any, Collection, Mapping
from . import NATIVE_MINT, SYSTEM_PROGRAM
from . import wire
from .b58 import Base58Error, b58decode, check_pubkey


class ReceiptError(ValueError):
    """Чек не того формата или противоречив — разбирать нельзя."""


class ReceiptIncomplete(ReceiptError):
    """Данных для атрибуции нет: суммы неизвестны (UNKNOWN_AMOUNT), не ноль."""


def _uint(x: Any, what: str) -> int:
    if isinstance(x, bool) or not isinstance(x, int) or x < 0:
        raise ReceiptError(f"{what}: не целое ≥ 0")
    return x


@dataclass(frozen=True)
class TokenAccountFlow:
    index: int
    address: str
    mint: str
    owner: str | None
    program: str | None
    decimals: int
    pre_raw: int | None               # None — до транзакции token-счёта не было
    post_raw: int | None              # None — после транзакции его нет (закрыт)
    pre_lamports: int
    post_lamports: int

    @property
    def created(self) -> bool:
        return self.pre_raw is None and self.post_raw is not None

    @property
    def closed(self) -> bool:
        return self.pre_raw is not None and self.post_raw is None

    @property
    def delta(self) -> int:
        return (self.post_raw or 0) - (self.pre_raw or 0)


@dataclass(frozen=True)
class SolTransfer:
    source: str
    dest: str
    lamports: int
    kind: str                          # transfer | create_account | create_account_with_seed
    level: str                         # top | inner


@dataclass(frozen=True)
class MintFlow:
    mint: str
    owner: str
    program: str
    delta_raw: int
    debit_raw: int
    credit_raw: int
    accounts: tuple[TokenAccountFlow, ...]
    outside: tuple[TokenAccountFlow, ...]   # наши счета того же mint вне сделки, изменившиеся в транзакции


@dataclass(frozen=True)
class NativeFlows:
    wallet: str
    wallet_pre: int
    wallet_post: int
    fee_lamports: int                  # meta.fee (base + priority), платит fee payer
    fee_paid_by_wallet: int
    rent_deposit_lamports: int         # в созданные наши token-счета (возвратный депозит)
    rent_refund_lamports: int          # из закрытых наших token-счетов
    wsol_delta_raw: int
    transfers_out: tuple[SolTransfer, ...]
    tip_lamports: int
    external_lamports: int             # ушло из домена кошелька сверх fee (tip, rent чужих счетов, SOL в своп)

    @property
    def wallet_delta(self) -> int:
        return self.wallet_post - self.wallet_pre

    @property
    def nonrefundable_lamports(self) -> int:
        """Безвозвратно: fee (priority уже внутри) + tip. Rent своих счетов сюда не входит (S17, S18)."""
        return self.fee_paid_by_wallet + self.tip_lamports


@dataclass(frozen=True)
class SwapAmounts:
    ok: bool
    in_raw: int                        # фактически списано (с учётом возврата роутера)
    out_raw: int                       # фактически получено нашими счетами сделки
    in_flow: MintFlow
    out_flow: MintFlow


@dataclass(frozen=True)
class Receipt:
    signature: str
    slot: int
    block_time: int | None
    version: str | int
    ok: bool
    err: Any
    fee_payer: str
    fee_lamports: int
    signers: tuple[str, ...]
    account_keys: tuple[str, ...]
    loaded_writable: tuple[str, ...]
    loaded_readonly: tuple[str, ...]
    recent_blockhash: str
    top_programs: tuple[str, ...]
    inner_programs: tuple[str, ...] | None   # None — RPC не отдал innerInstructions
    token_accounts: tuple[TokenAccountFlow, ...]
    pre_lamports: tuple[int, ...]
    post_lamports: tuple[int, ...]
    sol_transfers: tuple[SolTransfer, ...]
    compute_units: int | None

    def mint_flow(self, mint: str, *, owner: str, program: str,
                  accounts: Collection[str] | None = None) -> MintFlow:
        """Изменение mint на счетах owner (или только на счетах сделки accounts). Нужен owner и programId у
        каждой строки этого mint — иначе ReceiptIncomplete."""
        check_pubkey(mint, "mint")
        check_pubkey(owner, "владелец")
        rows = [a for a in self.token_accounts if a.mint == mint]
        if any(a.owner is None or a.program is None for a in rows):
            raise ReceiptIncomplete(f"{mint}: в чеке нет owner/programId у счёта — атрибуция невозможна")
        own = [a for a in rows if a.owner == owner]
        for a in own:
            if a.program != program:
                raise ReceiptError(f"счёт {a.address} mint {mint} под программой {a.program}, ожидалась {program}")
        deal = own if accounts is None else [a for a in own if a.address in accounts]
        outside = () if accounts is None else tuple(a for a in own if a.address not in accounts and a.delta)
        d = sum(a.delta for a in deal)
        return MintFlow(mint, owner, program, d, sum(-a.delta for a in deal if a.delta < 0),
                        sum(a.delta for a in deal if a.delta > 0), tuple(deal), outside)

    def swap_amounts(self, *, wallet: str, in_mint: str, in_program: str, out_mint: str, out_program: str,
                     accounts: Collection[str] | None = None) -> SwapAmounts:
        """Фактический вход/выход свопа ExactIn по счетам сделки (S14–S16)."""
        if in_mint == out_mint:
            raise ReceiptError("вход и выход — один mint")
        fin = self.mint_flow(in_mint, owner=wallet, program=in_program, accounts=accounts)
        fout = self.mint_flow(out_mint, owner=wallet, program=out_program, accounts=accounts)
        if fin.outside or fout.outside:
            names = ", ".join(a.address for a in fin.outside + fout.outside)
            raise ReceiptError(f"изменились наши счета вне сделки ({names}) — атрибуция не однозначна")
        if not self.ok:
            if fin.delta_raw or fout.delta_raw:
                raise ReceiptError("неуспешная транзакция изменила токены — разбору не верю")
            return SwapAmounts(False, 0, 0, fin, fout)
        if fin.delta_raw > 0:
            raise ReceiptError(f"вход {in_mint} прибыл, а не убыл")
        if fout.delta_raw < 0:
            raise ReceiptError(f"выход {out_mint} убыл, а не прибыл")
        return SwapAmounts(True, -fin.delta_raw, fout.delta_raw, fin, fout)

    def native(self, wallet: str, *, tip_accounts: Collection[str] = frozenset()) -> NativeFlows:
        if wallet not in self.account_keys:
            raise ReceiptError("кошелька нет среди ключей транзакции")
        wi = self.account_keys.index(wallet)
        own = [a for a in self.token_accounts if a.owner == wallet]
        if any(a.owner is None for a in self.token_accounts if a.mint == NATIVE_MINT):
            raise ReceiptIncomplete("у wSOL-счёта нет owner — SOL не разнести")
        dep = sum(a.post_lamports - (a.post_raw if a.mint == NATIVE_MINT else 0) for a in own if a.created)
        ref = sum(a.pre_lamports - (a.pre_raw if a.mint == NATIVE_MINT else 0) for a in own if a.closed)
        own_lam = sum(a.post_lamports - a.pre_lamports for a in own)
        wsol = sum(a.delta for a in own if a.mint == NATIVE_MINT)
        fee_w = self.fee_lamports if self.fee_payer == wallet else 0
        domain = (self.post_lamports[wi] - self.pre_lamports[wi]) + own_lam     # свой wSOL — внутри домена
        outs = tuple(t for t in self.sol_transfers if t.source == wallet)
        tip = sum(t.lamports for t in outs if t.dest in tip_accounts)
        return NativeFlows(wallet, self.pre_lamports[wi], self.post_lamports[wi], self.fee_lamports, fee_w, dep, ref,
                           wsol, outs, tip, -domain - fee_w)


def _sys_transfer(data: bytes, accs: list[str], level: str) -> SolTransfer | None:
    """System Transfer(2) / CreateAccount(0) / CreateAccountWithSeed(3) — сколько lamports и куда."""
    if len(data) < 4:
        return None
    tag = struct.unpack_from("<I", data, 0)[0]
    try:
        if tag == 2 and len(data) == 12 and len(accs) >= 2:
            return SolTransfer(accs[0], accs[1], struct.unpack_from("<Q", data, 4)[0], "transfer", level)
        if tag == 0 and len(data) == 52 and len(accs) >= 2:
            return SolTransfer(accs[0], accs[1], struct.unpack_from("<Q", data, 4)[0], "create_account", level)
        if tag == 3 and len(accs) >= 2:
            n = struct.unpack_from("<Q", data, 36)[0]          # base(32) после тега, затем длина seed u64
            off = 44 + n
            if len(data) == off + 8 + 8 + 32:
                return SolTransfer(accs[0], accs[1], struct.unpack_from("<Q", data, off)[0],
                                   "create_account_with_seed", level)
    except struct.error:
        return None
    return None


def parse_receipt(tx: Mapping | None, *, expect_signature: str | None = None) -> Receipt:
    if tx is None:
        raise ReceiptIncomplete("чека нет (getTransaction = null): суммы неизвестны, это не ноль")
    meta = tx.get("meta")
    if meta is None:
        raise ReceiptIncomplete("meta = null: суммы неизвестны")
    t = tx.get("transaction")
    if not isinstance(t, dict) or not isinstance(t.get("message"), dict):
        raise ReceiptError("transaction не в encoding=json")
    msg = t["message"]
    version = tx.get("version", "legacy")
    if version not in ("legacy", 0):
        raise ReceiptError(f"версия транзакции {version!r} не поддержана")
    static = msg.get("accountKeys")
    if not isinstance(static, list) or not all(isinstance(k, str) for k in static):
        raise ReceiptError("accountKeys не список строк (нужен encoding=json, не jsonParsed)")
    la = meta.get("loadedAddresses")
    if la is None:
        if version == 0 and msg.get("addressTableLookups"):
            raise ReceiptIncomplete("v0 с ALT без loadedAddresses — индексы не разрешить")
        la = {"writable": [], "readonly": []}
    lw, lr = list(la.get("writable") or []), list(la.get("readonly") or [])
    keys = tuple(static + lw + lr)
    for k in keys:
        check_pubkey(k, "ключ транзакции")
    sigs = t.get("signatures")
    if not isinstance(sigs, list) or not sigs:
        raise ReceiptError("нет подписей")
    if expect_signature is not None and sigs[0] != expect_signature:
        raise ReceiptError("чек другой транзакции (первая подпись не та)")
    header = msg.get("header") or {}
    nsig = _uint(header.get("numRequiredSignatures"), "numRequiredSignatures")
    if nsig != len(sigs) or nsig < 1:
        raise ReceiptError("число подписей не совпадает с заголовком")
    pre_b, post_b = meta.get("preBalances"), meta.get("postBalances")
    if not isinstance(pre_b, list) or not isinstance(post_b, list) or not len(pre_b) == len(post_b) == len(keys):
        raise ReceiptError("pre/postBalances не по числу ключей")
    pre_l = tuple(_uint(x, "preBalances") for x in pre_b)
    post_l = tuple(_uint(x, "postBalances") for x in post_b)

    def rows(name: str) -> dict[int, dict]:
        out: dict[int, dict] = {}
        src = meta.get(name)
        if src is None:
            raise ReceiptIncomplete(f"{name} нет — токены не разнести")
        for b in src:
            i = _uint(b.get("accountIndex"), "accountIndex")
            if i >= len(keys) or i in out:
                raise ReceiptError(f"{name}: индекс {i} вне ключей или повтор")
            amt = (b.get("uiTokenAmount") or {}).get("amount")
            if not (isinstance(amt, str) and amt.isdigit()):
                raise ReceiptError(f"{name}: amount не строка цифр")
            out[i] = {"mint": check_pubkey(b.get("mint"), "mint"), "owner": b.get("owner"),
                      "program": b.get("programId"), "raw": int(amt),
                      "dec": _uint((b.get("uiTokenAmount") or {}).get("decimals"), "decimals")}
            for k in ("owner", "program"):
                if out[i][k] is not None:
                    check_pubkey(out[i][k], k)
        return out

    pre_t, post_t = rows("preTokenBalances"), rows("postTokenBalances")
    flows = []
    for i in sorted(set(pre_t) | set(post_t)):
        a, b = pre_t.get(i), post_t.get(i)
        r = dict(b or a)
        if a and b:
            if (a["mint"], a["dec"]) != (b["mint"], b["dec"]):
                raise ReceiptError(f"счёт {keys[i]}: mint/decimals до и после различаются")
            for k in ("owner", "program"):
                if a[k] is not None and b[k] is not None and a[k] != b[k]:
                    raise ReceiptError(f"счёт {keys[i]}: {k} до и после различаются")
                r[k] = a[k] if a[k] == b[k] else None    # пропуск на одной стороне — неизвестно, не «взять другую»
        flows.append(TokenAccountFlow(i, keys[i], r["mint"], r["owner"], r["program"], r["dec"],
                                      a["raw"] if a else None, b["raw"] if b else None, pre_l[i], post_l[i]))

    def ix_list(src, level):
        out = []
        for ix in src:
            pi = _uint(ix.get("programIdIndex"), "programIdIndex")
            accs = ix.get("accounts")
            if pi >= len(keys) or not isinstance(accs, list) or any(_uint(a, "account") >= len(keys) for a in accs):
                raise ReceiptError("индекс инструкции вне ключей")
            out.append((keys[pi], [keys[a] for a in accs], ix.get("data"), level))
        return out

    top = ix_list(msg.get("instructions") or [], "top")
    inner_raw = meta.get("innerInstructions")
    inner = None if inner_raw is None else [x for grp in inner_raw for x in ix_list(grp.get("instructions") or [],
                                                                                    "inner")]
    transfers = []
    # у неуспешной транзакции переводы откатаны рантаймом: их инструкции — не движение SOL (удержана только fee)
    for prog, accs, data, level in ([] if meta.get("err") is not None else top + (inner or [])):
        if prog != SYSTEM_PROGRAM or not isinstance(data, str):
            continue
        try:
            raw = b58decode(data)
        except Base58Error:
            raise ReceiptError("данные инструкции не base58") from None
        tr = _sys_transfer(raw, accs, level)
        if tr is not None:
            transfers.append(tr)
    cu = meta.get("computeUnitsConsumed")
    return Receipt(
        signature=sigs[0], slot=_uint(tx.get("slot"), "slot"),
        block_time=tx.get("blockTime") if isinstance(tx.get("blockTime"), int) else None, version=version,
        ok=meta.get("err") is None, err=meta.get("err"), fee_payer=keys[0], fee_lamports=_uint(meta.get("fee"), "fee"),
        signers=keys[:nsig], account_keys=keys, loaded_writable=tuple(lw), loaded_readonly=tuple(lr),
        recent_blockhash=check_pubkey(msg.get("recentBlockhash"), "recentBlockhash"),
        top_programs=tuple(dict.fromkeys(p for p, *_ in top)),
        inner_programs=None if inner is None else tuple(dict.fromkeys(p for p, *_ in inner)),
        token_accounts=tuple(flows), pre_lamports=pre_l, post_lamports=post_l, sol_transfers=tuple(transfers),
        compute_units=cu if isinstance(cu, int) and not isinstance(cu, bool) else None)


def wire_of(tx_b64: Mapping) -> wire.WireTransaction:
    """getTransaction(encoding=base64) → разобранные байты: подписи, message, хеш сообщения."""
    t = tx_b64.get("transaction") if isinstance(tx_b64, Mapping) else None
    if not (isinstance(t, list) and len(t) == 2 and t[1] == "base64"):
        raise ReceiptError("transaction не в encoding=base64")
    try:
        return wire.decode(t[0], kind="tx", encoding="base64")
    except wire.WireError as e:
        raise ReceiptError(f"байты транзакции: {e}") from None


def plan_mismatches(rc: Receipt, *, signature: str, recent_blockhash: str, fee_payer: str,
                    message_hash: str | None = None, wire_tx: wire.WireTransaction | None = None) -> list[str]:
    """Сверка исполненного с журналом: подпись, blockhash, плательщик; при байтах — хеш сообщения (§9.3)."""
    out = []
    if rc.signature != signature:
        out.append("подпись чека не та")
    if rc.recent_blockhash != recent_blockhash:
        out.append("blockhash чека не тот, что подписан")
    if rc.fee_payer != fee_payer:
        out.append(f"плательщик {rc.fee_payer}, а не {fee_payer}")
    if message_hash is not None:
        if wire_tx is None:
            out.append("хеш сообщения не сверен: нет байтов транзакции")
        else:
            if wire_tx.signature != rc.signature:
                out.append("байты и чек — разные транзакции")
            if wire_tx.message.message_hash != message_hash:
                out.append("хеш исполненного сообщения ≠ закреплённому")
    return out
