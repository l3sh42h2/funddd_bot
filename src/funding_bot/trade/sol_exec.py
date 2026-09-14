"""Исполнитель спота Solana (ТЗ §9, §12; CODE_INTEGRATION §4 SpotExecutor): ОДНА выбранная роутером попытка свопа.

Маршрут здесь не выбирается (spot_router), хедж не делается (движок). Порядок попытки — запись-до на каждом шаге:
  журнал VALIDATED (закреплены message_hash, blockhash и ЕГО lastValidBlockHeight, слот blockhash)
  → подпись только проверенных байтов (sign.sign_checked: проверка и симуляция этих байтов, срок blockhash)
  → журнал SIGNED_DURABLE (байты в БД; сбой записи — отправки нет, X01)
  → журнал BROADCAST_ATTEMPTED → ОДНА отправка тех же байтов (ответ узла — свидетельство, не исход)
  → резолвер по всем RPC (null одного узла ничего не доказывает) → finalized-чек → фактические суммы по счетам
    сделки (с возвратом роутера: списано = ушло − вернулось) → чек и применение исполнения движком — одной
    транзакцией БД (apply вызывается внутри неё).
Исход — только доказанный: ok / failed (в сети с ошибкой: токены не двигались, сеть оплачена) / expired
(доказанно не вошла: высота > lastValidBlockHeight у ≥ 2 RPC и пустая история) / not_sent (подписи нет — в сеть уйти
не могла); всё прочее — unknown (в т.ч. confirmed без finalized и finalized без чека): движок не хеджирует по
котировке и не шлёт второй своп. Повтор — только те же подписанные байты (retransmit), никогда новая подпись.
Готовые транзакции провайдера с внешним подписантом (Jupiter Order) не исполняются: external_signer_recovery=false.
"""
from __future__ import annotations
import base64, json, logging, time
from dataclasses import dataclass, replace
from typing import Any, Callable, Iterable, Mapping, Sequence
from . import store
from .fees import lamports, receipt_components
from .keys import ModeForbidden, gate as mode_gate, redact
from .solana import NATIVE_MINT, journal as J, wire
from .solana.accounts import ata, ata_rent_lamports, read_token_account
from .solana.receipt import ReceiptError, parse_receipt, plan_mismatches
from .solana.resolver import resolve as resolve_attempt
from .solana.rpc import RpcError, SendUnknown
from .solana.sign import BlockhashRef, SignError, sign_checked

log = logging.getLogger(__name__)

POLL_S = 2.0                 # опрос статуса отправленной попытки (технический темп, не деньги)
WAIT_S = 90.0                # сколько ждать finalized в одном вызове: ~150 блоков срока blockhash + финальность
REBROADCAST_EVERY = 3        # те же байты ещё раз каждые N опросов, пока транзакция не видна в сети
PROVEN = ("ok", "failed", "expired", "not_sent")


class PresendRefused(RuntimeError):
    """Отправки не было и быть не могло: подпись не создана или не записана (журнал это доказывает)."""


@dataclass(frozen=True)
class SwapOutcome:
    """Итог попытки. in_raw/out_raw — факт по счетам сделки из finalized-чека; None — неизвестно (не 0)."""
    state: str                          # ok | failed | expired | not_sent | unknown
    attempt_id: str | None
    signature: str | None
    in_raw: int | None = None
    out_raw: int | None = None
    commitment: str | None = None
    reason: str = ""
    fees: tuple = ()                    # fees.FeeComponent из чека: meta.fee, tip, депозиты/возвраты rent
    receipt: str | None = None          # new | same | finality — первый ли раз применяется исполнение
    slot: int | None = None
    provider: str | None = None
    path: str | None = None

    @property
    def proven(self) -> bool:
        return self.state in PROVEN


class RpcChain:
    """spot_router.ChainState поверх RpcPool + слот ответа getLatestBlockhash для каждого выданного blockhash:
    без слота истечение не доказать (resolver: покрытие истории узлов), попытка осталась бы UNKNOWN навсегда."""

    def __init__(self, pool, commitment: str = "confirmed"):
        self.pool, self.commitment = pool, commitment
        self._slots: dict[str, int] = {}

    def latest_blockhash(self) -> tuple[str, int]:
        info = self.pool.read(lambda ep: ep.latest_blockhash(self.commitment))
        if len(self._slots) > 256:
            self._slots.clear()
        self._slots[info.blockhash] = info.context_slot
        return info.blockhash, info.last_valid_block_height

    def block_height(self) -> int | None:
        try:
            return self.pool.read(lambda ep: ep.block_height(self.commitment))
        except Exception as e:          # noqa — высота неизвестна: подписывать нельзя (sign.expiry_reasons)
            log.warning("sol: высота блока не прочитана: %s", type(e).__name__)
            return None

    def blockhash_slot(self, blockhash: str) -> int | None:
        return self._slots.get(blockhash)


def _plain(v: Any) -> Any:
    return v if v is None or isinstance(v, (bool, int, str)) else str(v)


def _fees(plan: Mapping, *, wallet: str, fee: int, tip: int, deposit: int, refund: int, external: Any,
          source: str) -> tuple:
    """Статьи чека: сеть, tip, депозит/возврат rent наших счетов и SOL, ушедший из домена кошелька на чужие счета
    (external − tip; депозиты и возвраты наших счетов в external не входят) — невозвратный расход (SPEC §182). Пара
    со SOL на входе/выходе: торговый SOL в том же external не отделить — статья «неизвестно», а не 0."""
    foreign: int | None = None
    if external is not None and NATIVE_MINT not in (plan.get("in_mint"), plan.get("out_mint")):
        foreign = max(0, int(external) - int(tip))
    out = receipt_components(fee_lamports=fee, fee_payer=wallet, tip_lamports=tip,
                             rent_deposits=(deposit,) if deposit else (), rent_refunds=(refund,) if refund else (),
                             rent_nonrefundable=foreign or 0, source=source)
    if foreign is None:
        out += (lamports("rent_nonrefundable", None, wallet, estimated=False, source=source,
                         note="SOL на чужие счета не разнесён"),)
    return out


class SolanaExecutor:
    """Спот-нога Solana для движка (связка sol_best_hyperliquid). Один кошелёк, одна книга: какой бы агрегатор ни
    выиграл клип, исполнение и балансы — здесь. endpoints — независимые RPC (≥ 2 для доказательства истечения),
    primary — узел отправки; chain — RpcChain; validator — sol_route_validator.RouteTxValidator (results_for)."""
    chain_name = "solana"

    def __init__(self, *, wallet: str, genesis: str, signer, endpoints: Sequence, chain, validator,
                 mode_state: Callable[[], tuple[str | None, bool]], clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Any] = time.sleep, poll_s: float = POLL_S, wait_s: float = WAIT_S,
                 min_sources: int = 2, tip_accounts: Iterable[str] = (), keys=None):
        if signer is not None and signer.public_key() != wallet:
            raise ValueError("ключ Solana даёт другой адрес, чем кошелёк связки")
        self.wallet, self.genesis, self.signer = wallet, genesis, signer
        self.endpoints = tuple(endpoints)
        if not self.endpoints:
            raise ValueError("нет RPC Solana")
        self.primary = self.endpoints[0]
        self.chain, self.validator = chain, validator
        self.mode_state, self.clock, self.sleep = mode_state, clock, sleep
        self.poll_s, self.wait_s, self.min_sources = float(poll_s), float(wait_s), int(min_sources)
        self.tip_accounts = frozenset(tip_accounts)
        self._keys = keys

    def __repr__(self) -> str:
        return f"SolanaExecutor({self.wallet}, rpc={[getattr(e, 'label', '?') for e in self.endpoints]})"

    # --- ворота -----------------------------------------------------------------------------------
    def _gate(self) -> str:
        """Своп — новое экономическое действие: на паузе и не в live не отправляется (hedge=False)."""
        try:
            mode, paused = self.mode_state()
        except Exception as e:          # noqa — режим/пауза не прочитаны — закрыто
            raise ModeForbidden(f"режим/пауза не прочитаны ({type(e).__name__}): отправка запрещена") from None
        if self._keys is not None:
            self._keys.gate(mode, "send", paused=bool(paused))
        else:
            mode_gate(mode, "send", paused=bool(paused))
        return "live"

    # --- чтения кошелька ---------------------------------------------------------------------------
    def _read(self, fn):
        last = None
        for ep in self.endpoints:
            try:
                return fn(ep)
            except Exception as e:      # noqa — следующий узел; все молчат — неизвестно
                last = e
        raise RuntimeError(f"ни один RPC не ответил: {type(last).__name__ if last else '?'}")

    def token_balance(self, mint: str, program: str) -> int | None:
        """Токены на ATA кошелька (по программе mint). Счёта нет — 0; не прочитано — None (не 0)."""
        addr = ata(self.wallet, mint, program)
        try:
            info = self._read(lambda ep: read_token_account(ep, addr))
        except Exception as e:          # noqa
            log.warning("sol: баланс %s не прочитан: %s", mint, redact(e))
            return None
        if info is None:
            return 0
        if info.mint != mint or info.owner != self.wallet or info.program != program or info.raw_diff:
            return None                 # чужой счёт по адресу ATA — баланс не наш (S08)
        return int(info.amount)

    def native_balance(self) -> int | None:
        try:
            return int(self._read(lambda ep: ep.balance(self.wallet))[0])
        except Exception as e:          # noqa
            log.warning("sol: баланс SOL не прочитан: %s", redact(e))
            return None

    def account_rent(self, mint: str, program: str, exts: Sequence[str] = ()) -> int | None:
        """0 — ATA уже есть; иначе депозит за его создание; None — неизвестно."""
        try:
            if self._read(lambda ep: read_token_account(ep, ata(self.wallet, mint, program))) is not None:
                return 0
            return int(self._read(lambda ep: ata_rent_lamports(ep, program, exts)))
        except Exception:               # noqa
            return None

    def mint_value(self, mint: str) -> dict | None:
        """getAccountInfo(mint, jsonParsed).value — для instruments.mint_mismatches (S03–S06). Сбой — исключение."""
        return self._read(lambda ep: ep.account_info(mint, encoding="jsonParsed"))[0]

    # --- журнал ------------------------------------------------------------------------------------
    @staticmethod
    def attempts(con, logical_action_id: str) -> list[dict]:
        return [J.attempt(con, r[0]) for r in con.execute(
            "SELECT attempt_id FROM sol_tx_attempts WHERE logical_action_id=? ORDER BY created", (logical_action_id,))]

    def touched(self, con, logical_action_id: str) -> bool:
        """Могла ли попытка уйти в сеть: подписанная (или дальше). VALIDATED / ABANDONED_UNSIGNED — не могла."""
        return any(a["state"] not in (J.S.VALIDATED, J.S.ABANDONED_UNSIGNED)
                   for a in self.attempts(con, logical_action_id))

    # --- своп --------------------------------------------------------------------------------------
    def swap(self, con, cand, req, *, logical_action_id: str, clip_ref: str | None, meta: Mapping,
             min_validity_heights: int | None, apply: Callable[[SwapOutcome], None]) -> SwapOutcome:
        """Исполнить выбранного кандидата. PresendRefused — ничего не ушло (журнал это доказывает). Иначе — итог;
        для доказанных исходов apply(итог) уже вызван внутри транзакции БД вместе с записью чека."""
        try:
            mode = self._gate()
        except ModeForbidden as e:
            raise PresendRefused(str(e)) from None
        if self.signer is None:
            raise PresendRefused("ключа Solana нет (readonly): отправка запрещена")
        p = cand.payload
        if p is None or p.kind != "message":
            raise PresendRefused("готовая транзакция провайдера (внешний подписант) — только показ, не исполняю")
        if cand.message_hash is None or cand.recent_blockhash is None or cand.last_valid_block_height is None:
            raise PresendRefused("кандидат не собран: нет message_hash или пары blockhash/высота")
        if not cand.required_signers or any(s != self.wallet for s in cand.required_signers):
            raise PresendRefused("подписант не наш кошелёк")
        msg = p.raw_bytes()
        if wire.message_hash(msg) != cand.message_hash:
            raise PresendRefused("байты не те, что проверены (хеш сообщения)")
        got = self.validator.results_for(cand.message_hash) if self.validator is not None else None
        if got is None:
            raise PresendRefused("эти байты не проверены и не симулированы — не подписываю")
        val, sim = got
        height = self.chain.block_height()
        slot_fn = getattr(self.chain, "blockhash_slot", None)
        bslot = slot_fn(cand.recent_blockhash) if slot_fn is not None else None
        plan = {k: _plain(v) for k, v in dict(meta).items()}
        plan.update(side=req.side, in_mint=req.input.mint, in_program=req.input.program,
                    in_account=req.input_account or ata(self.wallet, req.input.mint, req.input.program),
                    out_mint=req.output.mint, out_program=req.output.program,
                    out_account=req.output_account or ata(self.wallet, req.output.mint, req.output.program),
                    amount_in_raw=int(cand.amount_in_raw), expected_out_raw=cand.expected_out_raw,
                    min_out_raw=cand.effective_min_out, request_hash=cand.request_hash)
        try:
            self._gate()  # block-height/slot reads may have outlived the first admission check
        except ModeForbidden as e:
            raise PresendRefused(str(e)) from None
        prev = [a for a in self.attempts(con, logical_action_id)]
        try:
            aid = J.new_attempt(con, network=self.genesis, wallet=self.wallet, logical_action_id=logical_action_id,
                                provider=cand.provider, path=cand.path, payload_kind="message",
                                message_hash=cand.message_hash, recent_blockhash=cand.recent_blockhash,
                                last_valid_block_height=int(cand.last_valid_block_height), lvbh_exact=True,
                                blockhash_slot=bslot, plan=plan, clip_ref=clip_ref, request_id=cand.request_id,
                                predecessor_attempt_id=prev[-1]["attempt_id"] if prev else None, now=self.clock())
        except J.JournalError as e:
            raise PresendRefused(f"журнал: {e}") from None
        try:
            signed = sign_checked(self.signer, msg, val, sim,
                                  blockhash=BlockhashRef(cand.recent_blockhash, int(cand.last_valid_block_height)),
                                  block_height=height, min_validity_heights=min_validity_heights)
        except SignError as e:
            J.abandon_unsigned(con, aid, str(e)[:200], now=self.clock())
            raise PresendRefused(str(e)) from None
        try:
            sig = J.record_signed(con, aid, signed, now=self.clock())
        except Exception as e:          # noqa — подпись не записана: отправлять НЕЛЬЗЯ (X01)
            try:
                J.abandon_unsigned(con, aid, f"подпись не записана: {type(e).__name__}", now=self.clock())
            except Exception:           # noqa — VALIDATED останется: не подписана в журнале — в сеть не уходила
                log.error("sol: попытка %s не переведена в ABANDONED_UNSIGNED", aid)
            raise PresendRefused(f"подпись не записана в журнал ({type(e).__name__}) — не отправляю") from None
        del signed
        try:
            raw = J.mark_broadcast(con, aid, endpoint=getattr(self.primary, "label", "rpc"), now=self.clock())
        except Exception as e:          # noqa — «отправляю» не записано: не отправляем; исход — по сроку blockhash
            return SwapOutcome("unknown", aid, sig, reason=f"отметка отправки не записана ({type(e).__name__}) — "
                               "не отправлял", provider=cand.provider, path=cand.path)
        self._send(con, aid, sig, raw, mode)
        return self._wait(con, aid, apply, self.clock() + self.wait_s)

    def _send(self, con, aid: str, sig: str, raw: bytes, mode: str) -> None:
        b64 = base64.b64encode(raw).decode()
        try:
            self.primary.send_transaction(b64, expected_signature=sig, mode=mode)
            res, detail = "accepted", ""
        except SendUnknown as e:
            res, detail = "no_response", str(e)
        except RpcError as e:
            res, detail = "rejected", str(e)            # отказ узла — не доказательство, что байтов нет в сети
        except Exception as e:          # noqa — любой обрыв после начала отправки = исход неизвестен
            res, detail = "no_response", type(e).__name__
        try:
            J.record_send_result(con, aid, result=res, detail=redact(detail)[:300], now=self.clock())
        except Exception:               # noqa — свидетельство не записано: исход всё равно выясняет резолвер
            log.error("sol: ответ узла на отправку %s не записан", aid)

    def _retransmit(self, con, aid: str, mode: str) -> bool:
        """Те же подписанные байты ещё раз (X02) — только пока попытка в полёте. Новой подписи не бывает."""
        try:
            row = J.attempt(con, aid)
            if row["state"] not in (J.S.SIGNED_DURABLE, J.S.BROADCAST_ATTEMPTED, J.S.UNKNOWN):
                return False
            raw = J.mark_broadcast(con, aid, endpoint=getattr(self.primary, "label", "rpc"), now=self.clock())
        except Exception:               # noqa
            return False
        self._send(con, aid, row["signature"], raw, mode)
        return True

    def _wait(self, con, aid: str, apply, deadline: float, *, retransmit: bool = True) -> SwapOutcome:
        polls = 0
        while True:
            out = self._resolve_once(con, aid, apply)
            if out.state != "unknown" or self.clock() >= deadline:
                return out
            polls += 1
            if retransmit and out.commitment is None and polls % REBROADCAST_EVERY == 0:
                try:
                    self._retransmit(con, aid, self._gate())
                except ModeForbidden:   # пауза/режим: повтор тех же байтов — не новое действие, но и не нужен
                    pass
            self.sleep(self.poll_s)

    def resolve(self, con, attempt_id: str, *, apply: Callable[[SwapOutcome], None],
                wait_s: float | None = None, retransmit: bool = True) -> SwapOutcome:
        """Исход попытки после рестарта или UNKNOWN (X02–X05): только чтение сети и те же байты. VALIDATED без подписи
        уйти в сеть не могла — ABANDONED_UNSIGNED и not_sent."""
        row = J.attempt(con, attempt_id)
        if row["state"] == J.S.VALIDATED:
            out = SwapOutcome("not_sent", attempt_id, None, reason="не подписана — в сеть не уходила",
                              provider=row["provider"], path=row["path"])
            with store.tx(con):
                J.abandon_unsigned(con, attempt_id, "перезапуск: попытка не подписана", now=self.clock())
                apply(out)
            return out
        deadline = self.clock() + (self.wait_s if wait_s is None else float(wait_s))
        return self._wait(con, attempt_id, apply, deadline, retransmit=retransmit)

    # --- исход -------------------------------------------------------------------------------------
    def _stored_receipt(self, con, row: Mapping) -> SwapOutcome | None:
        """Finalized-чек, уже записанный в журнал: исполнение применяется из него без сети (идемпотентно)."""
        r = con.execute("SELECT flows_json, fee_lamports, slot, ok, err_json FROM sol_receipts WHERE network=? AND "
                        "signature=? AND logical_leg='swap' AND commitment='finalized'",
                        (row["network"], row["signature"])).fetchone()
        if r is None:
            return None
        try:
            f = json.loads(r[0])
            fees = _fees(json.loads(row["plan_json"] or "{}"), wallet=self.wallet, fee=int(f["fee_paid_by_wallet"]),
                         tip=int(f["tip"]), deposit=int(f["rent_deposit"]), refund=int(f["rent_refund"]),
                         external=f.get("external"), source=f"receipt:{row['path']}")
            ok = bool(r[3])
            return SwapOutcome("ok" if ok else "failed", row["attempt_id"], row["signature"], int(f["in_raw"]),
                               int(f["out_raw"]), "finalized", "" if ok else f"в сети с ошибкой: {str(r[4])[:120]}",
                               fees, "same", r[2], row["provider"], row["path"])
        except (KeyError, TypeError, ValueError):
            return None

    def _resolve_once(self, con, aid: str, apply) -> SwapOutcome:
        row = J.attempt(con, aid)
        st, sig = row["state"], row["signature"]
        base = dict(attempt_id=aid, signature=sig, provider=row["provider"], path=row["path"])
        done = None
        if st == J.S.ABANDONED_UNSIGNED:
            done = SwapOutcome("not_sent", reason="не подписана", **base)
        elif st == J.S.EXPIRED_NOT_LANDED:
            done = SwapOutcome("expired", reason="доказанно не вошла в блок", **base)
        elif st in (J.S.FINALIZED_OK, J.S.FINALIZED_ERR):
            done = self._stored_receipt(con, row)
        elif st == J.S.ROLLED_BACK:
            # был confirmed, резолвер доказал: в канонической цепи нет и войти уже не может (срок blockhash). Хедж
            # ставится только после finalized, токены не двигались — это доказанное неисполнение, а не UNKNOWN.
            if self._stored_receipt(con, row) is not None:
                return SwapOutcome("unknown", reason="откат confirmed, но есть finalized-чек — ручная сверка", **base)
            done = SwapOutcome("expired", reason="confirmed откатился — в цепи нет", **base)
        if done is not None:            # исход уже в журнале: применить (движок идемпотентен) — без сети
            with store.tx(con):
                apply(done)
            return done
        try:
            res = resolve_attempt(J.attempt_ref(row), self.endpoints, min_sources=self.min_sources)
            new = J.apply_resolution(con, aid, res, now=self.clock())
        except J.JournalError as e:
            return SwapOutcome("unknown", reason=f"журнал: {e}", **base)
        except Exception as e:          # noqa — резолвер не должен падать; упал — исход неизвестен
            return SwapOutcome("unknown", reason=f"резолвер: {type(e).__name__}", **base)
        if new == J.S.EXPIRED_NOT_LANDED:
            out = SwapOutcome("expired", reason=res.reason, **base)
            with store.tx(con):
                apply(out)
            return out
        if new in (J.S.FINALIZED_OK, J.S.FINALIZED_ERR):
            tx = res.tx
            if tx is None or res.commitment != "finalized":
                tx = self._finalized_tx(sig)
            if tx is None:
                return SwapOutcome("unknown", commitment="finalized", reason="finalized, но чека нет — суммы "
                                   "неизвестны (UNKNOWN_AMOUNT)", **base)
            return self._receipt(con, row, tx, apply)
        landed = new in (J.S.CONFIRMED_OK, J.S.CONFIRMED_ERR)
        return SwapOutcome("unknown", commitment="confirmed" if landed else None,
                           reason=("в сети (confirmed), жду finalized" if landed else res.reason), **base)

    def _finalized_tx(self, sig: str) -> dict | None:
        for ep in self.endpoints:
            try:
                tx = ep.transaction(sig, commitment="finalized")
            except Exception:           # noqa
                continue
            if tx:
                return tx
        return None

    def _receipt(self, con, row: Mapping, tx: dict, apply) -> SwapOutcome:
        aid, sig = row["attempt_id"], row["signature"]
        base = dict(attempt_id=aid, signature=sig, provider=row["provider"], path=row["path"])
        try:
            plan = json.loads(row["plan_json"])
            rc = parse_receipt(tx, expect_signature=sig)
            mism = plan_mismatches(rc, signature=sig, recent_blockhash=row["recent_blockhash"], fee_payer=self.wallet)
            if mism:
                return SwapOutcome("unknown", commitment="finalized", reason="чек не сходится с журналом: " +
                                   "; ".join(mism), **base)
            amounts = rc.swap_amounts(wallet=self.wallet, in_mint=plan["in_mint"], in_program=plan["in_program"],
                                      out_mint=plan["out_mint"], out_program=plan["out_program"],
                                      accounts={plan["in_account"], plan["out_account"]})
            native = rc.native(self.wallet, tip_accounts=self.tip_accounts)
        except (ReceiptError, KeyError, ValueError, TypeError) as e:
            return SwapOutcome("unknown", commitment="finalized", reason=f"чек не разобран: {e}"[:300], **base)
        if amounts.ok and amounts.in_raw > int(plan.get("amount_in_raw") or 0):
            return SwapOutcome("unknown", commitment="finalized", reason="списано больше ExactIn — ручная сверка",
                               **base)
        fees = _fees(plan, wallet=self.wallet, fee=native.fee_paid_by_wallet, tip=native.tip_lamports,
                     deposit=native.rent_deposit_lamports, refund=native.rent_refund_lamports,
                     external=native.external_lamports, source=f"receipt:{row['path']}")
        out = SwapOutcome("ok" if amounts.ok else "failed", in_raw=amounts.in_raw, out_raw=amounts.out_raw,
                          commitment="finalized", fees=fees, slot=rc.slot,
                          reason="" if amounts.ok else f"в сети с ошибкой: {json.dumps(rc.err)[:120]}", **base)
        try:
            with store.tx(con):
                kind = J.record_receipt(con, attempt_id=aid, logical_leg="swap", receipt=rc, amounts=amounts,
                                        native=native, commitment="finalized", now=self.clock())
                out = replace(out, receipt=kind)
                apply(out)
        except J.JournalError as e:
            return SwapOutcome("unknown", commitment="finalized", reason=f"чек не принят журналом: {e}"[:300], **base)
        return out

    def pending(self, con) -> list[dict]:
        """Попытки кошелька, чей исход не применён (для старта и перед новым свопом: X10, R12)."""
        return [r for r in J.unresolved(con) if r["wallet"] == self.wallet] + [
            J.attempt(con, r[0]) for r in con.execute(
                "SELECT attempt_id FROM sol_tx_attempts WHERE wallet=? AND state=?", (self.wallet, str(J.S.VALIDATED)))]
