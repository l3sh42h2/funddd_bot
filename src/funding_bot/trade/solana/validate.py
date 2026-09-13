"""Проверка транзакции свопа до подписи (ТЗ §9.1, SOLANA_ROUTERS §6, read_solana §2; S10–S13, R09, R14).

validate(msg, intent) — ПОЛНОЕ сообщение после раскрытия ALT (message.ResolvedMessage), не только статические ключи:
  - подписанты ровно {наш кошелёк}, плательщик наш, blockhash — тот, чей lastValidBlockHeight у нас (G08);
  - каждая инструкция верхнего уровня разобрана (decoders.decode_top) и разрешена манифестом v1:
      ComputeBudget — лимит/цена/размер данных, каждая не более раза, без счетов;
      ATA Create/CreateIdempotent — плательщик и владелец наши, адрес = ATA(владелец, mint, программа mint), счёт —
        наш вход/выход; временный wSOL-счёт (создан и закрыт в этой же транзакции на наш кошелёк) — только wSOL;
      System Transfer — только с нашего кошелька: tip получателю из allowlist владельца или пополнение нашего
        wSOL-счёта; durable nonce, CreateAccount и прочее — отказ;
      Token/Token-2022 — SyncNative и CloseAccount только временного wSOL-счёта; Approve, SetAuthority, Transfer,
        Burn, чужой CloseAccount и прочее — отказ (S12);
      роутер: ровно одна инструкция свопа (Jupiter route_v2/shared_accounts_route_v2, OKX swap_v3/_with_cpi_event/
        swap_tob_v3) по IDL: ExactIn in_amount = наш, порог в аргументах (Jupiter ⌈quoted·(10⁴−slip)/10⁴⌉, OKX
        min_return) ≥ нашего min_out (R09), platform/positive slippage/commission/trim = 0, источник и получатель —
        наши счета, mint и программы — из запроса, служебные счета — по IDL и PDA;
      всё прочее (чужая программа, ALT-инструкция, Memo, ExactOut, другой роутер) — отказ (S10);
  - защищённые счета кошелька (protected_accounts) не встречаются ни writable, ни в маршруте;
  - сеть = 5000·подписи + ⌈цена·лимит/10⁶⌉ (лимит без SetComputeUnitLimit — 200 000 на инструкцию, потолок 1.4M,
    верхняя оценка) ≤ лимита владельца; tip ≤ лимита и только известным получателям (R14); сеть + tip + rent +
    пополнение wSOL ≤ native-бюджета. Нет лимита — «limit_missing:…», а не «без ограничений».
check_simulation(sim, intent, pre, msg=, plan=) — результат simulateTransaction ТОЧНЫХ байтов (sigVerify=false,
replaceRecentBlockhash=false, accounts = кошелёк, вход, выход, защищённые; innerInstructions=true) (S13):
  - нет поля err, нет счетов или вложенных инструкций — «simulation_unavailable…», а не успех; err ≠ null — отказ
    «simulation_failed:…» (отдельно от «нет маршрута/ликвидности» роутера; порог программы — min_out_not_reached);
  - эффекты: вход списан ≤ amount_in, выход получил ≥ min_out, делегат/close authority не появились, защищённые
    счета не изменились, кошелёк потерял не больше плана (с учётом переводов SOL из маршрута на чужие счета);
  - вложенные инструкции: переводы токенов с нашей подписью — только с входного (и временного wSOL) счёта в сумме
    ≤ amount_in; approve/setAuthority/closeAccount/burn нашей подписью, assign/allocate кошелька — отказ.
    Вложенные программы AMM не в allowlist: их id пишутся в details (режим наблюдения, read_solana §2.3 п.3).
Лимиты (cu_*, tip, сеть, native budget) задаёт только владелец: None = не задано = запрещено.

REFUSING — валидатор не подключён: ok=False с причиной, ничего не может быть подписано.
"""
from __future__ import annotations
import base64, json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence
from . import NATIVE_MINT, SYSTEM_PROGRAM, TOKEN_PROGRAM, TOKEN_PROGRAMS
from .accounts import AccountError, ata as derive_ata, parse_token_account_raw
from .b58 import is_pubkey
from .borsh import BorshError
from .decoders import (DEFAULT_CU_PER_IX, JUPITER_PROGRAM, LAMPORTS_PER_SIGNATURE, MAX_TX_COMPUTE_UNITS, OKX_ROUTER,
                       PROGRAM_NAMES, Decoded, DecodeError, decode_inner, decode_top, jupiter_idl, okx_idl, pda)
from .message import ALT_PROGRAM, ResolvedMessage

MANIFEST_VERSION = "sol_tx_manifest_v1"
JUP_SWAPS = ("route_v2", "shared_accounts_route_v2")
OKX_SWAPS = ("swap_v3", "swap_v3_with_cpi_event", "swap_tob_v3")
OKX_PREP = ("create_token_account", "create_token_account_with_seed", "wrap_unwrap_v3")
CB_ALLOWED = ("SetComputeUnitLimit", "SetComputeUnitPrice", "SetLoadedAccountsDataSizeLimit")
MIN_OUT_ERROR = 6001            # Jupiter SlippageToleranceExceeded / OKX MinReturnNotReached (IDL errors)
_TOKEN_DANGER = ("approve", "approveChecked", "revoke", "setAuthority", "closeAccount", "burn", "burnChecked",
                 "freezeAccount", "thawAccount", "mintTo", "mintToChecked")


@dataclass(frozen=True)
class SwapIntent:
    wallet: str
    input_mint: str
    input_program: str
    input_account: str
    output_mint: str
    output_program: str
    output_account: str
    amount_in_raw: int
    min_out_raw: int
    cu_limit_cap: int | None            # None — отдельного потолка нет (протокольный 1.4M и лимит сети — всегда)
    cu_price_cap_micro_lamports: int | None
    tip_cap_lamports: int | None        # None — любой tip > 0 запрещён
    native_budget_lamports: int | None  # None — не задано: запрещено
    max_network_fee_lamports: int | None = None      # 5000·подписи + приоритет; None — запрещено
    tip_recipients: tuple[str, ...] = ()             # allowlist владельца; пусто — tip никому
    account_rent: tuple[tuple[str, int | None], ...] = ()   # наш счёт → депозит rent при создании (0 — уже есть)
    protected_accounts: tuple[str, ...] = ()         # прочие счета кошелька: не трогать
    recent_blockhash: str | None = None              # blockhash, чей lastValidBlockHeight известен (G08)
    router_program: str | None = None                # роутер провайдера кандидата

    def rent_of(self, account: str) -> int | None:
        for a, v in self.account_rent:
            if a == account:
                return v
        return None

    @property
    def ours(self) -> frozenset[str]:
        return frozenset((self.wallet, self.input_account, self.output_account, *self.protected_accounts))


@dataclass(frozen=True)
class Validation:
    ok: bool
    reasons: tuple[str, ...]
    message_hash: str | None
    manifest_version: str | None
    details: Mapping[str, Any] = field(default_factory=dict, compare=False)


class TransactionValidator(Protocol):
    def validate(self, msg: ResolvedMessage, intent: SwapIntent) -> Validation: ...

    def check_simulation(self, sim: Mapping, intent: SwapIntent, pre: Mapping, **kw) -> Validation: ...


class _Refusing:
    _WHY = ("валидатор до подписи не подключён (ManifestValidator) — ждёт solders",)

    def validate(self, msg: ResolvedMessage, intent: SwapIntent) -> Validation:
        return Validation(False, self._WHY, None, None)

    def check_simulation(self, sim: Mapping, intent: SwapIntent, pre: Mapping, **kw) -> Validation:
        return Validation(False, self._WHY, None, None)


REFUSING = _Refusing()


def sim_accounts(intent: SwapIntent) -> tuple[str, ...]:
    """Счета для simulateTransaction.accounts и для чтения pre — в этом порядке."""
    return tuple(dict.fromkeys((intent.wallet, intent.input_account, intent.output_account,
                                *intent.protected_accounts)))


def _intent_reasons(i: SwapIntent) -> list[str]:
    r = []
    for name in ("wallet", "input_mint", "input_program", "input_account", "output_mint", "output_program",
                 "output_account"):
        if not is_pubkey(getattr(i, name)):
            r.append(f"intent_invalid:{name}")
    if i.input_program not in TOKEN_PROGRAMS or i.output_program not in TOKEN_PROGRAMS:
        r.append("intent_invalid:token_program")
    if len({i.wallet, i.input_account, i.output_account}) != 3 or i.input_mint == i.output_mint:
        r.append("intent_invalid:accounts")
    for name in ("amount_in_raw", "min_out_raw"):
        v = getattr(i, name)
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            r.append(f"intent_invalid:{name}")
    if set(i.protected_accounts) & {i.wallet, i.input_account, i.output_account}:
        r.append("intent_invalid:protected_accounts")
    return r


def _idl_fixed(dec: Decoded, idl_accounts: Sequence[Mapping], r: list[str], tag: str) -> None:
    """Счета с фиксированным адресом в IDL (event_authority, программы, wSOL mint) — ровно он или заглушка."""
    for a in idl_accounts:
        want = a.get("address")
        if want is None:
            continue
        got = dec.accounts.get(a["name"])
        if got != want and not (a.get("optional") and got == dec.program_id):
            r.append(f"{tag}_fixed_account:{a['name']}")


class ManifestValidator:
    """TransactionValidator манифеста v1. IDL — из пакета (idl/*.json), sha256 закреплён в decoders."""

    def __init__(self):
        try:
            jup, okx = jupiter_idl(), okx_idl()
        except (OSError, BorshError) as e:      # IDL нет или хеш другой — ничего не проверено
            raise RuntimeError(f"IDL роутеров не загружены: {e}") from None
        self.manifest_version = f"{MANIFEST_VERSION}+jup:{jup.sha256[:12]}+okx:{okx.sha256[:12]}"
        self.okx_sa = pda([b"okx_sa"], OKX_ROUTER)
        self.okx_event = pda([b"__event_authority"], OKX_ROUTER)

    # --- статическая проверка ---
    def validate(self, msg: ResolvedMessage, intent: SwapIntent) -> Validation:
        r: list[str] = _intent_reasons(intent)
        d: dict[str, Any] = {"manifest": self.manifest_version}
        if r:
            return Validation(False, tuple(r), msg.message_hash, self.manifest_version, d)
        w = intent.wallet
        if msg.version not in ("legacy", 0):
            r.append(f"message_version:{msg.version}")
        if msg.payer != w:
            r.append("fee_payer")
        if w not in msg.signers:
            r.append("wallet_not_signer")
        r += [f"extra_signer:{s}" for s in msg.signers if s != w]              # S10, G14
        if intent.recent_blockhash is not None and msg.recent_blockhash != intent.recent_blockhash:
            r.append("blockhash_mismatch")
        for s in msg.alts:
            if s.owner != ALT_PROGRAM:
                r.append(f"alt_owner:{s.address}")
            if s.deactivation_slot is not None:
                r.append(f"alt_deactivated:{s.address}")
        d["alts"] = [{"address": s.address, "slot": s.slot, "n": len(s.addresses), "content_hash": s.content_hash}
                     for s in msg.alts]
        if len(set(msg.keys)) != len(msg.keys):
            r.append("keys_duplicate")
        decs: list[Decoded | None] = []
        for i, ix in enumerate(msg.instructions):
            try:
                decs.append(decode_top(ix.program_id, ix.data, ix.accounts))
            except DecodeError as e:
                r.append(f"ix_not_in_manifest:{i}:{PROGRAM_NAMES.get(ix.program_id, ix.program_id)}:{e.code}")
                decs.append(None)
        temp = self._temp_accounts(decs, w)
        d["temp_accounts"] = sorted(temp)
        cb: dict[str, int] = {}
        routers: list[tuple[int, Decoded]] = []
        tips: list[tuple[str, int]] = []
        rent = wrap = 0
        created: set[str] = set()
        for i, x in enumerate(decs):
            if x is None:
                continue
            p = x.program
            if p == "ComputeBudget":
                if x.name in cb:
                    r.append("cu_conflict")
                if x.remaining:
                    r.append("cb_accounts")
                if x.name not in CB_ALLOWED:
                    r.append(f"ix_not_in_manifest:{i}:ComputeBudget:{x.name}")
                cb[x.name] = next(iter(x.args.values()), 0)
            elif p == "ATA":
                rent += self._ata(i, x, intent, temp, created, r)
            elif p == "System":
                wrap += self._system(i, x, intent, temp, tips, r)
            elif p in ("Token", "Token2022"):
                self._token(i, x, intent, temp, r)
            elif p == "Jupiter":
                routers.append((i, x))
                self._jupiter(x, intent, r, d)
            elif p == "OKX":
                if x.name in OKX_SWAPS:
                    routers.append((i, x))
                self._okx(x, intent, r, d)
            else:
                r.append(f"ix_not_in_manifest:{i}:{p}")
        if len(routers) != 1:
            r.append(f"router_ix_count:{len(routers)}")
        elif intent.router_program is not None and routers[0][1].program_id != intent.router_program:
            r.append(f"router_program:{routers[0][1].program}")
        if routers:
            d["router_index"] = routers[0][0]
            for a in routers[0][1].remaining:
                if a in intent.protected_accounts:
                    r.append(f"protected_account_in_route:{a}")
        r += [f"protected_account_writable:{a}" for a in intent.protected_accounts if a in msg.writable]
        fee = self._fees(msg, decs, cb, intent, r, d)
        tip_total = sum(v for _, v in tips)
        if tip_total and intent.tip_cap_lamports is None:
            r.append("limit_missing:max_tip_lamports_per_tx")
        elif tip_total and tip_total > intent.tip_cap_lamports:
            r.append("tip_over_cap")
        native_total = fee + tip_total + rent + wrap
        if intent.native_budget_lamports is None:
            r.append("limit_missing:native_budget_lamports")
        elif native_total > intent.native_budget_lamports:
            r.append("native_budget_exceeded")
        d.update(tips=tips, tip_lamports=tip_total, rent_lamports=rent, wrap_lamports=wrap, native_total=native_total)
        reasons = tuple(dict.fromkeys(r))
        return Validation(not reasons, reasons, msg.message_hash, self.manifest_version, d)

    @staticmethod
    def _temp_accounts(decs: Sequence[Decoded | None], w: str) -> set[str]:
        """Временный wSOL-счёт: наш ATA wSOL, созданный и закрытый (на наш кошелёк) в этой же транзакции."""
        made: dict[str, int] = {}
        out: set[str] = set()
        for i, x in enumerate(decs):
            if x is None:
                continue
            if (x.program == "ATA" and x.name in ("Create", "CreateIdempotent") and x.accounts.get("mint") == NATIVE_MINT
                    and x.accounts.get("wallet") == w and x.accounts.get("tokenProgram") == TOKEN_PROGRAM):
                made.setdefault(x.accounts["account"], i)
            if (x.program == "Token" and x.name == "closeAccount" and x.accounts.get("account") in made
                    and made[x.accounts["account"]] < i and x.accounts.get("destination") == w
                    and x.accounts.get("owner") == w):
                out.add(x.accounts["account"])
        return out

    @staticmethod
    def _ata(i: int, x: Decoded, it: SwapIntent, temp: set[str], created: set[str], r: list[str]) -> int:
        if x.name == "RecoverNested":
            r.append(f"ix_not_in_manifest:{i}:ATA:RecoverNested")
            return 0
        a = x.accounts
        acc, owner, mint, prog = a["account"], a["wallet"], a["mint"], a["tokenProgram"]
        if a["payer"] != it.wallet:
            r.append("ata_payer")
        if owner != it.wallet:
            r.append("ata_owner")
        if a["systemProgram"] != SYSTEM_PROGRAM:
            r.append("ata_system_program")
        if acc in created:
            r.append("ata_duplicate")
        created.add(acc)
        if acc == it.input_account:
            want = (it.input_mint, it.input_program)
        elif acc == it.output_account:
            want = (it.output_mint, it.output_program)
        elif acc in temp:
            want = (NATIVE_MINT, TOKEN_PROGRAM)
        else:
            r.append("ata_not_intent_account")
            return 0
        if (mint, prog) != want:
            r.append("ata_mint_or_program")
        elif owner == it.wallet and derive_ata(owner, mint, prog) != acc:     # независимый вывод PDA (S07)
            r.append("ata_address")
        if acc in temp:
            return 0                                   # депозит вернётся закрытием в этой же транзакции
        v = it.rent_of(acc)
        if v is None:
            r.append(f"rent_unknown:{acc}")
            return 0
        return v

    @staticmethod
    def _system(i: int, x: Decoded, it: SwapIntent, temp: set[str], tips: list, r: list[str]) -> int:
        if x.name == "advanceNonce":
            r.append("durable_nonce")                  # срок по blockhash, не по nonce: иначе истечение не доказать
            return 0
        if x.name != "transfer":
            r.append(f"ix_not_in_manifest:{i}:System:{x.name}")
            return 0
        src, dst, lam = x.accounts["source"], x.accounts["destination"], x.args["lamports"]
        if src != it.wallet:
            r.append("system_transfer_foreign_source")
            return 0
        if dst in it.tip_recipients:
            tips.append((dst, lam))
            return 0
        wsol_in = it.input_mint == NATIVE_MINT and dst == it.input_account
        if dst in temp or wsol_in:
            return lam if wsol_in else 0               # временный счёт вернёт всё закрытием
        r.append(f"system_transfer:{dst}")             # R14: перевод вне tip/wrap — внешняя плата мимо котировки
        return 0

    @staticmethod
    def _token(i: int, x: Decoded, it: SwapIntent, temp: set[str], r: list[str]) -> None:
        a = x.accounts
        wsol = {it.input_account} if it.input_mint == NATIVE_MINT else set()
        if x.name == "syncNative" and (a.get("account") in temp or a.get("account") in wsol):
            return
        if x.name == "closeAccount" and a.get("account") in temp:
            if a.get("destination") == it.wallet and a.get("owner") == it.wallet:
                return
        if x.name == "closeAccount":
            r.append("token_close_forbidden")          # чужой/прежний счёт: не закрываем
            return
        r.append(f"token_ix_forbidden:{x.name}")       # Approve, SetAuthority, Transfer, Burn… (S12)

    def _common_route(self, x: Decoded, it: SwapIntent, r: list[str], *, authority: str, source: str, dest: str,
                      src_mint: str, dst_mint: str) -> None:
        g = x.accounts
        if g.get(authority) != it.wallet:
            r.append("ix_authority")
        if g.get(source) != it.input_account:
            r.append("ix_source")
        if g.get(dest) != it.output_account:
            r.append("ix_recipient")
        if g.get(src_mint) != it.input_mint or g.get(dst_mint) != it.output_mint:
            r.append("ix_mint")

    def _jupiter(self, x: Decoded, it: SwapIntent, r: list[str], d: dict) -> None:
        if x.name not in JUP_SWAPS:
            r.append(f"jupiter_ix_forbidden:{x.name}")    # ExactOut, V1, token_ledger, claim, close_token…
            return
        g, a = x.accounts, x.args
        v2 = x.name == "route_v2"
        self._common_route(x, it, r, authority="user_transfer_authority",
                           source="user_source_token_account" if v2 else "source_token_account",
                           dest="user_destination_token_account" if v2 else "destination_token_account",
                           src_mint="source_mint", dst_mint="destination_mint")
        if g["source_token_program"] != it.input_program or g["destination_token_program"] != it.output_program:
            r.append("ix_token_program")
        if v2:
            if g["destination_token_account"] not in (JUPITER_PROGRAM, it.output_account):   # заглушка = id программы
                r.append("ix_recipient")
        else:
            if g["program_authority"] != pda([b"authority", bytes([a["id"]])], JUPITER_PROGRAM):
                r.append("jupiter_program_authority")
            for k in ("program_source_token_account", "program_destination_token_account"):
                if g[k] in it.ours:
                    r.append(f"jupiter_program_account:{k}")
        _idl_fixed(x, jupiter_idl().accounts(x.name), r, "jupiter")
        q, s = a["quoted_out_amount"], a["slippage_bps"]
        if a["in_amount"] != it.amount_in_raw:
            r.append("ix_in_amount")
        if q <= 0 or not 0 <= s < 10000:
            r.append("ix_quote_args")
        if a["platform_fee_bps"]:
            r.append("platform_fee")
        if a["positive_slippage_bps"]:
            r.append("positive_slippage")
        if not a["route_plan"]:
            r.append("route_empty")
        min_out = -(-q * (10000 - s) // 10000)      # правило jupiter_spot (сверено с otherAmountThreshold 13.09)
        if min_out < it.min_out_raw:
            r.append("ix_min_out")
        d.update(router=f"jupiter:{x.name}", onchain_min_out=min_out, onchain_min_out_floor=q * (10000 - s) // 10000,
                 quoted_out=q, slippage_bps=s, route_steps=len(a["route_plan"]))

    def _okx(self, x: Decoded, it: SwapIntent, r: list[str], d: dict) -> None:
        g = x.accounts
        if x.name in OKX_PREP:
            if NATIVE_MINT not in (it.input_mint, it.output_mint):
                r.append(f"okx_prep_not_needed:{x.name}")   # USDC↔токен: подготовка wSOL роутером не нужна
                return
            if g.get("payer") != it.wallet or g.get("owner", it.wallet) != it.wallet:
                r.append("okx_prep_owner")
            args = x.args.get("args") or {}
            if args.get("commission_info") or args.get("platform_fee_rate"):
                r.append("okx_commission")
            for k in ("commission_account", "platform_fee_account"):
                if k in g and g[k] != OKX_ROUTER:
                    r.append(f"okx_fee_account:{k}")
            _idl_fixed(x, okx_idl().accounts(x.name), r, "okx")
            return
        if x.name not in OKX_SWAPS:
            r.append(f"okx_ix_forbidden:{x.name}")          # *_with_receiver, proxy_swap, claim, swap, enhanced…
            return
        sa = x.args["args"]
        self._common_route(x, it, r, authority="payer", source="source_token_account",
                           dest="destination_token_account", src_mint="source_mint", dst_mint="destination_mint")
        for k in ("commission_account", "platform_fee_account"):
            if g[k] != OKX_ROUTER:
                r.append(f"okx_fee_account:{k}")
        if g["sa_authority"] not in (OKX_ROUTER, self.okx_sa):
            r.append("okx_sa_authority")
        for k in ("source_token_sa", "destination_token_sa"):
            if g[k] in it.ours:
                r.append(f"okx_sa_account:{k}")
        if (g["source_token_program"] not in (OKX_ROUTER, it.input_program)
                or g["destination_token_program"] not in (OKX_ROUTER, it.output_program)):
            r.append("ix_token_program")
        if x.name == "swap_v3_with_cpi_event" and (g["event_authority"] != self.okx_event or g["program"] != OKX_ROUTER):
            r.append("okx_event_authority")
        _idl_fixed(x, okx_idl().accounts(x.name), r, "okx")
        if sa["amount_in"] != it.amount_in_raw:
            r.append("ix_in_amount")
        if not sa["routes"] or len(sa["amounts"]) != len(sa["routes"]) or any(not hop for hop in sa["routes"]):
            r.append("okx_routes_shape")
        if sum(sa["amounts"]) != sa["amount_in"]:
            r.append("okx_amounts_sum")                     # on-chain 6005; проверяем до подписи
        if sa["min_return"] < it.min_out_raw:
            r.append("ix_min_out")
        if x.args["commission_info"]:
            r.append("okx_commission")
        if x.args["platform_fee_rate"]:
            r.append("okx_platform_fee")
        if x.args.get("trim_rate"):
            r.append("okx_trim")
        d.update(router=f"okx:{x.name}", onchain_min_out=sa["min_return"], expected_out=sa["expect_amount_out"],
                 route_hops=len(sa["routes"]))

    @staticmethod
    def _fees(msg: ResolvedMessage, decs: Sequence[Decoded | None], cb: Mapping[str, int], it: SwapIntent,
              r: list[str], d: dict) -> int:
        n_other = sum(1 for x in decs if x is None or x.program != "ComputeBudget")
        limit = cb.get("SetComputeUnitLimit")
        eff = limit if limit is not None else min(MAX_TX_COMPUTE_UNITS, DEFAULT_CU_PER_IX * n_other)
        if eff > MAX_TX_COMPUTE_UNITS:
            r.append("cu_limit_over_max")
        if it.cu_limit_cap is not None and eff > it.cu_limit_cap:
            r.append("cu_limit_over_cap")
        price = cb.get("SetComputeUnitPrice", 0)
        if it.cu_price_cap_micro_lamports is not None and price > it.cu_price_cap_micro_lamports:
            r.append("cu_price_over_cap")
        priority = -(-price * eff // 1_000_000)
        base = LAMPORTS_PER_SIGNATURE * len(msg.signers)
        fee = base + priority
        if it.max_network_fee_lamports is None:
            r.append("limit_missing:max_network_fee_lamports_per_tx")
        elif fee > it.max_network_fee_lamports:
            r.append("network_fee_over_cap")
        d.update(cu_limit=limit, cu_limit_effective=eff, cu_price_micro_lamports=price, base_lamports=base,
                 priority_lamports=priority, fee_lamports=fee)
        return fee

    # --- эффекты симуляции ---
    def check_simulation(self, sim: Mapping, intent: SwapIntent, pre: Mapping, *, msg: ResolvedMessage | None = None,
                         plan: Validation | None = None) -> Validation:
        r: list[str] = []
        d: dict[str, Any] = {}
        mh = plan.message_hash if plan is not None else (msg.message_hash if msg is not None else None)

        def done() -> Validation:
            reasons = tuple(dict.fromkeys(r))
            return Validation(not reasons, reasons, mh, self.manifest_version, d)

        if plan is not None and not plan.ok:
            r.append("static_validation_failed")
        if not isinstance(sim, Mapping) or "err" not in sim:
            r.append("simulation_unavailable")              # нет ответа ≠ успех (S13)
            return done()
        d["units"] = sim.get("unitsConsumed")
        logs = sim.get("logs") or []
        d["logs_truncated"] = any("Log truncated" in str(x) for x in logs)
        err = sim["err"]
        if err is not None:
            r.append(self._sim_error(err, plan))
            d["logs_tail"] = [str(x)[:160] for x in logs[-5:]]
            return done()
        if sim.get("replacementBlockhash"):
            r.append("simulation_replaced_blockhash")      # симуляция не тех байтов, что будут подписаны
        addrs = sim_accounts(intent)
        posts = sim.get("accounts")
        if not isinstance(posts, list) or len(posts) != len(addrs):
            r.append("simulation_accounts_missing")
            return done()
        try:
            post = {a: _state(v) for a, v in zip(addrs, posts)}
            before = {a: _state(pre.get(a)) if isinstance(pre, Mapping) else None for a in addrs}
        except (AccountError, ValueError, TypeError) as e:
            r.append(f"simulation_account_unparsed:{type(e).__name__}")
            return done()
        self._effects(intent, before, post, plan, r, d)
        inner = sim.get("innerInstructions")
        if not isinstance(inner, list):
            r.append("simulation_inner_missing")           # вложенные переводы не проверить — не успех
            return done()
        self._inner(inner, intent, msg, plan, r, d)
        fee = sim.get("fee")
        if isinstance(fee, int) and plan is not None and isinstance(plan.details.get("fee_lamports"), int):
            if fee > plan.details["fee_lamports"]:
                r.append("sim_fee_over_plan")
        if (isinstance(d["units"], int) and plan is not None
                and isinstance(plan.details.get("cu_limit_effective"), int)
                and d["units"] > plan.details["cu_limit_effective"]):
            r.append("sim_units_over_limit")
        return done()

    @staticmethod
    def _sim_error(err: Any, plan: Validation | None) -> str:
        ie = err.get("InstructionError") if isinstance(err, Mapping) else None
        if (isinstance(ie, list) and len(ie) == 2 and plan is not None and ie[0] == plan.details.get("router_index")
                and isinstance(ie[1], Mapping) and ie[1].get("Custom") == MIN_OUT_ERROR):
            return "simulation_failed:min_out_not_reached"      # цена ушла: порог программы, не «нет ликвидности»
        return "simulation_failed:" + json.dumps(err, sort_keys=True, separators=(",", ":"))[:80]

    @staticmethod
    def _effects(it: SwapIntent, before: Mapping, post: Mapping, plan: Validation | None, r: list[str],
                 d: dict) -> None:
        w0, w1 = before.get(it.wallet), post.get(it.wallet)
        if w0 is None or w1 is None:
            r.append("sim_wallet_missing")
        else:
            if w1["owner"] != SYSTEM_PROGRAM or w1["data_len"]:
                r.append("sim_wallet_changed")
            d["wallet_lamports_drop"] = w0["lamports"] - w1["lamports"]
        i0, i1 = before.get(it.input_account), post.get(it.input_account)
        if i0 is None or i0["token"] is None:
            r.append("sim_input_pre_missing")
        elif i1 is None or i1["token"] is None:
            r.append("sim_input_closed")
        else:
            t0, t1 = i0["token"], i1["token"]
            if (i1["owner"], t1["mint"], t1["owner"]) != (it.input_program, it.input_mint, it.wallet):
                r.append("sim_input_account")
            spent = t0["amount"] - t1["amount"]
            d["input_spent"] = spent
            if spent > it.amount_in_raw:
                r.append("sim_input_overspent")
            if t1["delegate"] or t1["close_authority"]:
                r.append("sim_input_authority")
        o0, o1 = before.get(it.output_account), post.get(it.output_account)
        if o1 is None or o1["token"] is None:
            r.append("sim_output_missing")
        else:
            t1 = o1["token"]
            if (o1["owner"], t1["mint"], t1["owner"]) != (it.output_program, it.output_mint, it.wallet):
                r.append("sim_output_account")
            got = t1["amount"] - (o0["token"]["amount"] if o0 is not None and o0["token"] is not None else 0)
            d["output_received"] = got
            if got < it.min_out_raw:
                r.append("sim_output_below_min")
            if t1["delegate"] or t1["close_authority"]:
                r.append("sim_output_authority")
        for a in it.protected_accounts:
            if before.get(a) != post.get(a):
                r.append(f"sim_protected_changed:{a}")

    @staticmethod
    def _inner(inner: list, it: SwapIntent, msg: ResolvedMessage | None, plan: Validation | None, r: list[str],
               d: dict) -> None:
        keys = msg.keys if msg is not None else None
        temp = set(plan.details.get("temp_accounts", ())) if plan is not None else set()
        ours, w = it.ours, it.wallet
        spent = sol_foreign = 0
        foreign: list[tuple[str, int]] = []
        nested: set[str] = set()
        for grp in inner:
            ixs = grp.get("instructions") if isinstance(grp, Mapping) else None
            if not isinstance(ixs, list):
                r.append("simulation_inner_unparsed")
                continue
            for raw in ixs:
                x = decode_inner(raw, keys)
                info = x.info
                if x.program_id in TOKEN_PROGRAMS:
                    if x.name is None:
                        if w in x.accounts or ours & set(x.accounts):
                            r.append(f"inner_unparsed_ours:{x.error or 'token'}")
                        continue
                    by_wallet = w in {info.get(k) for k in ("authority", "multisigAuthority", "owner")}
                    touched = {info.get(k) for k in ("account", "source")} & (ours | temp)
                    if x.name in ("transfer", "transferChecked"):
                        if by_wallet:
                            src = info.get("source")
                            if src == it.input_account:
                                spent += _amount(info)
                            elif src not in temp:
                                r.append(f"inner_transfer_from_wallet:{src}")
                    elif x.name in _TOKEN_DANGER and (by_wallet or touched):
                        if not (x.name == "closeAccount" and info.get("account") in temp
                                and info.get("destination") == w):
                            r.append(f"inner_{x.name}")
                elif x.program_id == SYSTEM_PROGRAM:
                    if x.name is None:
                        if w in x.accounts:
                            r.append(f"inner_unparsed_ours:{x.error or 'system'}")
                        continue
                    if x.name in ("transfer", "createAccount", "createAccountWithSeed", "transferWithSeed"):
                        if w in (info.get("source"), info.get("sourceBase")):
                            dst = info.get("destination") or info.get("newAccount")
                            lam = int(info.get("lamports") or 0)
                            if dst not in ours and dst not in temp:
                                sol_foreign += lam
                                foreign.append((str(dst), lam))
                    elif x.name in ("assign", "allocate", "assignWithSeed", "allocateWithSeed"):
                        if info.get("account") == w:
                            r.append("inner_system_on_wallet")
                    elif w in (info.get("nonceAuthority"), info.get("authority")):
                        r.append(f"inner_{x.name}")
                elif x.error and w in x.accounts:
                    r.append(f"inner_unparsed_ours:{x.error}")
                else:
                    nested.add(x.program_id)
        if spent > it.amount_in_raw:
            r.append("inner_transfer_over_amount")
        d.update(inner_spent=spent, sol_out_foreign=sol_foreign, sol_foreign_recipients=foreign,
                 nested_programs=sorted(nested - set(PROGRAM_NAMES)))
        planned = plan.details.get("native_total") if plan is not None else None
        drop = d.get("wallet_lamports_drop")
        if it.native_budget_lamports is None:
            r.append("limit_missing:native_budget_lamports")
        elif isinstance(drop, int) and drop > it.native_budget_lamports:
            r.append("sim_native_spend")
        # SOL маршрута на чужие счета (R14, SPEC §182): чистый уход = фактическая просадка кошелька − план (сеть, tip,
        # rent наших счетов, пополнение wSOL). Сумма переводов не отличает временный счёт, закрытый обратно на кошелёк
        # в той же транзакции, от ухода насовсем; бюджет (сеть + кап rent) расходом маршрута не становится. Лимита
        # владельца на такой расход нет — любой положительный чистый уход — отказ до подписи.
        if isinstance(drop, int) and planned is not None:
            net = drop - planned
            d["sol_out_foreign_net"] = max(net, 0)
            if net > 0:
                r += [f"route_foreign_sol:{a}" for a, _ in foreign] or ["sim_native_spend"]
        elif sol_foreign:                          # просадку не сравнить с планом — возврат не доказан
            r += [f"route_foreign_sol:{a}" for a, _ in foreign]


def _amount(info: Mapping) -> int:
    v = info.get("amount")
    if v is None and isinstance(info.get("tokenAmount"), Mapping):
        v = info["tokenAmount"].get("amount")
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ValueError("сумма перевода не читается") from None


def _state(v: Mapping | None) -> dict | None:
    """Счёт из simulateTransaction.accounts / getMultipleAccounts(base64) → lamports, программа, token-поля."""
    if v is None:
        return None
    if not isinstance(v, Mapping) or not isinstance(v.get("lamports"), int):
        raise ValueError("счёт без lamports")
    data = v.get("data")
    if not (isinstance(data, (list, tuple)) and len(data) == 2 and data[1] == "base64"):
        raise ValueError("данные счёта не base64")
    raw = base64.b64decode(data[0], validate=True)
    owner = v.get("owner")
    tok = None
    if owner in TOKEN_PROGRAMS and len(raw) >= 165:
        t = parse_token_account_raw(raw, owner)
        tok = {k: t[k] for k in ("mint", "owner", "amount", "delegate", "close_authority", "state")}
    return {"lamports": v["lamports"], "owner": owner, "data_len": len(raw), "data": raw, "token": tok}
