"""Мост роутера спота к Solana-ядру (ТЗ §9.1, S10–S13, R09, R14).

Роутер — spot_router.PayloadValidator.validate(UnsignedTx, SwapCandidate, QuoteRequest) → причины (() — прошло);
ядро — solana/validate.TransactionValidator (validate по полному сообщению + check_simulation эффектов).

RouteTxValidator.validate:
  1) SwapIntent из запроса и кандидата: наши счета, ExactIn, порог = max(JSON провайдера, аргумент инструкции из JSON)
     — подписанное сообщение обязано держать не меньше того, что обещано (R09); лимиты сети/tip/CU/native —
     только владельца (None → «limit_missing»), получатели tip — allowlist политики;
  2) байты UnsignedTx: свой разбор + хеш message сверяется с закреплённым (подписываются только проверенные байты);
  3) полное сообщение (message.RpcMessageResolver: ALT по RPC, второй разбор solders) → ManifestValidator.validate;
  4) эффекты: EffectsSimulator читает pre наших счетов и симулирует ТОЧНЫЕ байты (sigVerify=false,
     replaceRecentBlockhash=false, accounts, innerInstructions) → check_simulation. Нет симулятора эффектов —
     «simulation_effects_unavailable» (недоступная обязательная симуляция не считается успешной, S13).
  Итоги (статическая проверка и эффекты) хранятся по message_hash: results_for() → вход для sign.sign_checked.
По умолчанию resolver/validator — message.UNAVAILABLE и validate.REFUSING: ничего не подключено — отказ.

EffectsSimulator — ещё и spot_router.Simulator: для байтов, уже проверенных validate(), отдаёт результат той же
симуляции (одна симуляция на сообщение); для прочих (прикидка CU в цикле Build) — обычная simulateTransaction.
SoldersAssembler — spot_router.TxAssembler: MessageV0 со своим payer/blockhash; таблицы ALT — только из сети
(AltCache), содержимое от провайдера — лишь сверка; SetComputeUnitLimit добавляется ровно один раз.
"""
from __future__ import annotations
import base64, json, threading
from collections import OrderedDict
from typing import Any, Mapping, Sequence
from .spot_router import (COMPUTE_BUDGET_PROGRAM, Ix, Payload, PayloadError, QuoteRequest, RouteLimits, RoutingPolicy,
                          SimResult, SolanaTools, SwapCandidate, UnsignedTx)
from .solana import WaitsSolders, wire
from .solana.decoders import JUPITER_PROGRAM, MAX_TX_COMPUTE_UNITS, OKX_ROUTER
from .solana.message import UNAVAILABLE, AltCache, MessageError, MessageResolver, RpcMessageResolver, SoldersMessageBuilder
from .solana.validate import REFUSING, ManifestValidator, SwapIntent, TransactionValidator, Validation, sim_accounts

ROUTER_OF = {"jupiter": JUPITER_PROGRAM, "okx": OKX_ROUTER}
MAX_RESULTS = 64


def swap_intent(cand: SwapCandidate, req: QuoteRequest, limits: RouteLimits, *, cu_limit_cap: int | None,
                cu_price_cap_micro_lamports: int | None, native_budget_lamports: int | None,
                tip_recipients: Sequence[str] = (), protected_accounts: Sequence[str] = (),
                recent_blockhash: str | None = None) -> tuple[SwapIntent | None, tuple[str, ...]]:
    """Что обязана сделать транзакция: наши счета, ExactIn, порог из инструкции роутера. Неизвестное — отказ."""
    r = []
    if cand.request_hash != req.request_hash:
        r.append("request_mismatch")
    if req.input_account is None:
        r.append("source_unverified")
    if req.output_account is None:
        r.append("recipient_unverified")
    if not cand.effective_min_out:
        r.append("no_min_out")
    if r:
        return None, tuple(r)
    min_out = max(x for x in (cand.min_out_raw, cand.onchain_min_out_raw) if x is not None)
    by_mint = {req.input.mint: req.input_account, req.output.mint: req.output_account}
    rent = tuple((by_mint[m], v) for m, v in req.account_rent if m in by_mint)
    return SwapIntent(wallet=req.wallet, input_mint=req.input.mint, input_program=req.input.program,
                      input_account=req.input_account, output_mint=req.output.mint, output_program=req.output.program,
                      output_account=req.output_account, amount_in_raw=req.amount_in_raw,
                      min_out_raw=min_out, cu_limit_cap=cu_limit_cap,
                      cu_price_cap_micro_lamports=cu_price_cap_micro_lamports,
                      tip_cap_lamports=limits.max_tip_lamports_per_tx, native_budget_lamports=native_budget_lamports,
                      max_network_fee_lamports=limits.max_network_fee_lamports_per_tx,
                      tip_recipients=tuple(tip_recipients), account_rent=rent,
                      protected_accounts=tuple(protected_accounts), recent_blockhash=recent_blockhash,
                      router_program=ROUTER_OF.get(cand.provider)), ()


def wire_tx(tx: UnsignedTx) -> wire.WireTransaction:
    """Байты на проверку: готовая транзакция как есть; своё message — с пустыми слотами подписей."""
    p = tx.payload
    if p.kind == "tx":
        return wire.parse_transaction(p.raw_bytes())
    if p.kind == "message":
        raw = p.raw_bytes()
        msg = wire.parse_message(raw)
        sigs = (wire.ZERO_SIG,) * msg.num_required_signatures
        return wire.WireTransaction(sigs, msg, wire.assemble(sigs, raw))
    raise PayloadError("список инструкций не собран — проверять нечего")


def _on(rpc: Any, fn):
    """SolanaRpc — как есть; RpcPool — первый здоровый узел (read с переключением)."""
    return rpc.read(fn) if hasattr(rpc, "read") else fn(rpc)


def _short(err: Any) -> str:
    return json.dumps(err, sort_keys=True, separators=(",", ":"), default=str)[:120]


class EffectsSimulator:
    """spot_router.Simulator + проверка эффектов для RouteTxValidator. rpc — SolanaRpc или RpcPool."""

    def __init__(self, rpc: Any, *, commitment: str = "confirmed", max_cache: int = MAX_RESULTS):
        self.rpc, self.commitment, self.max_cache = rpc, commitment, max_cache
        self._lock = threading.Lock()
        self._sims: OrderedDict[str, SimResult] = OrderedDict()
        self.calls = 0

    def _remember(self, mh: str, s: SimResult) -> None:
        with self._lock:
            self._sims[mh] = s
            self._sims.move_to_end(mh)
            while len(self._sims) > self.max_cache:
                self._sims.popitem(last=False)

    def simulate(self, tx: UnsignedTx) -> SimResult:
        with self._lock:
            hit = self._sims.get(tx.message_hash)
        if hit is not None:
            return hit
        b64 = base64.b64encode(wire_tx(tx).raw).decode()
        self.calls += 1
        res = _on(self.rpc, lambda ep: ep.simulate(b64, sig_verify=False, replace_recent_blockhash=False,
                                                   inner=False, commitment=self.commitment))
        return SimResult(ok="err" in res and res["err"] is None, err=None if res.get("err") is None else _short(res["err"]),
                         units_consumed=res.get("unitsConsumed"), slot=res.get("context_slot"))

    def check(self, tx: UnsignedTx, wtx: wire.WireTransaction, msg, intent: SwapIntent, plan: Validation,
              validator: TransactionValidator) -> Validation:
        addrs = list(sim_accounts(intent))
        b64 = base64.b64encode(wtx.raw).decode()

        def run(ep):
            values, _ = ep.multiple_accounts(addrs, encoding="base64", commitment=self.commitment)
            return values, ep.simulate(b64, sig_verify=False, replace_recent_blockhash=False, accounts=addrs,
                                       inner=True, commitment=self.commitment)
        self.calls += 1
        try:
            values, res = _on(self.rpc, run)
        except Exception as e:      # noqa: BLE001 — узел не ответил: симуляции нет, это не успех
            return Validation(False, (f"simulation_unavailable:{type(e).__name__}",), tx.message_hash,
                              plan.manifest_version)
        pre = dict(zip(addrs, values)) if isinstance(values, list) and len(values) == len(addrs) else {}
        v = validator.check_simulation(res, intent, pre, msg=msg, plan=plan)
        if isinstance(res, Mapping) and "err" in res:
            self._remember(tx.message_hash, SimResult(ok=res["err"] is None and v.ok,
                                                      err=None if res["err"] is None else _short(res["err"]),
                                                      units_consumed=res.get("unitsConsumed"),
                                                      slot=res.get("context_slot")))
        return v


class RouteTxValidator:
    """PayloadValidator роутера поверх валидатора Solana-ядра. Подключается в SolanaTools(validator=...)."""

    def __init__(self, resolver: MessageResolver = UNAVAILABLE, validator: TransactionValidator = REFUSING, *,
                 limits: RouteLimits, cu_limit_cap: int | None = None, cu_price_cap_micro_lamports: int | None = None,
                 native_budget_lamports: int | None = None, tip_recipients: Sequence[str] = (),
                 protected_accounts: Sequence[str] = (), effects: EffectsSimulator | None = None,
                 require_effects: bool = True):
        self.resolver, self.validator, self.limits = resolver, validator, limits
        self.caps = dict(cu_limit_cap=cu_limit_cap, cu_price_cap_micro_lamports=cu_price_cap_micro_lamports,
                         native_budget_lamports=native_budget_lamports, tip_recipients=tuple(tip_recipients),
                         protected_accounts=tuple(protected_accounts))
        self.effects, self.require_effects = effects, require_effects
        self._lock = threading.Lock()
        self._results: OrderedDict[str, tuple[Validation, Validation]] = OrderedDict()

    def results_for(self, message_hash: str) -> tuple[Validation, Validation] | None:
        """(статическая проверка, эффекты симуляции) для этих байтов — вход sign.sign_checked."""
        with self._lock:
            return self._results.get(message_hash)

    def validate(self, tx: UnsignedTx, cand: SwapCandidate, req: QuoteRequest) -> tuple[str, ...]:
        intent, why = swap_intent(cand, req, self.limits, recent_blockhash=tx.recent_blockhash, **self.caps)
        if why:
            return why
        try:
            wtx = wire_tx(tx)
        except PayloadError:
            return ("payload_not_assembled",)
        except wire.WireError:
            return ("wire_unparsed",)
        mh = wtx.message.message_hash
        if mh != tx.message_hash:
            return ("message_hash_mismatch",)            # проверяем не те байты, что закреплены под подпись
        try:
            msg = self.resolver.resolve(wtx)
        except WaitsSolders:
            return ("alt_resolver_waits_solders",)
        except MessageError as e:
            return (f"alt_resolver_error:{e.code}",)
        except Exception as e:      # noqa — RPC/ALT: не проверено
            return (f"alt_resolver_error:{type(e).__name__}",)
        if msg.message_hash != mh:
            return ("message_hash_mismatch",)
        try:
            v = self.validator.validate(msg, intent)
        except WaitsSolders:
            return ("validator_waits_solders",)
        except Exception as e:      # noqa
            return (f"validator_error:{type(e).__name__}",)
        if not v.ok:
            return tuple(v.reasons) or ("validator_refused",)
        if v.reasons:                                    # ok=True с причинами — противоречие, не пропускаем
            return tuple(v.reasons)
        if v.message_hash is not None and v.message_hash != mh:
            return ("message_hash_mismatch",)
        if v.manifest_version is None:
            return ("manifest_version_unknown",)         # проверено неизвестно чем
        if self.effects is None:
            return ("simulation_effects_unavailable",) if self.require_effects else ()
        try:
            s = self.effects.check(tx, wtx, msg, intent, v, self.validator)
        except Exception as e:      # noqa
            return (f"simulation_effects_error:{type(e).__name__}",)
        if not s.ok or s.reasons:
            return tuple(s.reasons) or ("simulation_refused",)
        if s.message_hash != mh:
            return ("message_hash_mismatch",)
        with self._lock:
            self._results[mh] = (v, s)
            while len(self._results) > MAX_RESULTS:
                self._results.popitem(last=False)
        return ()


class SoldersAssembler:
    """spot_router.TxAssembler на solders. alts — AltCache того же узла, что симулирует и отправляет."""

    def __init__(self, alts: AltCache, builder: SoldersMessageBuilder | None = None):
        self.alts, self.builder = alts, builder or SoldersMessageBuilder()

    def assemble(self, *, payer: str, ixs: Sequence[Ix], alts: Sequence[tuple[str, tuple[str, ...]]],
                 recent_blockhash: str, last_valid_block_height: int, cu_limit: int | None) -> UnsignedTx:
        ixs = list(ixs)
        if cu_limit is not None:
            if isinstance(cu_limit, bool) or not isinstance(cu_limit, int) or not 0 < cu_limit <= MAX_TX_COMPUTE_UNITS:
                raise PayloadError(f"cu_limit {cu_limit!r} вне 1..{MAX_TX_COMPUTE_UNITS}")
            if any(ix.program_id == COMPUTE_BUDGET_PROGRAM and ix.data[:1] == b"\x02" for ix in ixs):
                raise PayloadError("SetComputeUnitLimit уже есть в инструкциях провайдера")
            ixs.insert(0, Ix(COMPUTE_BUDGET_PROGRAM, (), b"\x02" + cu_limit.to_bytes(4, "little")))
        order = list(dict.fromkeys(a for a, _ in alts))
        snaps = self.alts.get({a: -1 for a in order}) if order else {}
        for addr, content in alts:
            if content and tuple(content) != snaps[addr].addresses[:len(content)]:
                raise PayloadError(f"ALT {addr}: содержимое провайдера не совпадает с сетью")
        raw = self.builder.build_v0(payer=payer, instructions=ixs, recent_blockhash=recent_blockhash,
                                    alts=[snaps[a] for a in order])
        m = wire.parse_message(raw)
        return UnsignedTx(Payload("message", "base64", data=base64.b64encode(raw).decode()), m.message_hash,
                          m.signers, recent_blockhash, last_valid_block_height, cu_limit)

    def wrap(self, payload: Payload, last_valid_block_height: int | None) -> UnsignedTx:
        """Готовая транзакция провайдера как есть (Order, сверка OKX /swap) — без модификации."""
        if payload.kind == "tx":
            m = wire.parse_transaction(payload.raw_bytes()).message
        elif payload.kind == "message":
            m = wire.parse_message(payload.raw_bytes())
        else:
            raise PayloadError("список инструкций: сначала сборка")
        cu = None
        for ix in m.instructions:
            if m.static_keys[ix.program_index] == COMPUTE_BUDGET_PROGRAM and ix.data[:1] == b"\x02" and len(ix.data) == 5:
                cu = int.from_bytes(ix.data[1:5], "little")
        return UnsignedTx(payload, m.message_hash, m.signers, m.recent_blockhash, last_valid_block_height, cu)


def solana_tools(rpc: Any, chain: Any, *, limits: RouteLimits, policy: RoutingPolicy,
                 native_budget_lamports: int | None, cu_limit_cap: int | None = None,
                 cu_price_cap_micro_lamports: int | None = None, protected_accounts: Sequence[str] = (),
                 alt_cache: AltCache | None = None) -> tuple[SolanaTools, RouteTxValidator]:
    """Боевой набор для адаптеров: сборка, симуляция с эффектами, полная проверка. Один узел/пул на всё."""
    cache = alt_cache or AltCache(rpc)
    effects = EffectsSimulator(rpc)
    v = RouteTxValidator(RpcMessageResolver(cache), ManifestValidator(), limits=limits, cu_limit_cap=cu_limit_cap,
                         cu_price_cap_micro_lamports=cu_price_cap_micro_lamports,
                         native_budget_lamports=native_budget_lamports, tip_recipients=policy.tip_recipients,
                         protected_accounts=protected_accounts, effects=effects)
    return SolanaTools(assembler=SoldersAssembler(cache), simulator=effects, chain=chain, validator=v), v
