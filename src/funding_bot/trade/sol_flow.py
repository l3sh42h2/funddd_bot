"""Потоки связки sol_best_hyperliquid для Desk и Engine (ТЗ SOL×HL §6–§11; объём пилота — план приёмки):
лонг токена в Solana за USDC через лучший из проверенных маршрутов (Jupiter Build V2 / OKX V6; Jupiter Order — только
показ) × шорт Hyperliquid dex:МОНЕТА. BSC/Aster сюда не заходит: Desk и Engine передают сюда только сделки и команды
этой связки (runtime.is_sol_deal), глобальные CHAIN/VENUE здесь не используются вовсе.

Инструмент — из реестра (instruments.json), не из таблицы коллектора: mint с регистром, программа токена, USDC,
Fs/Fp, dex:МОНЕТА и scope счёта HL замораживаются в сделке (schema 2) и сверяются перед каждым действием.

Вход — ОДИН клип (пилот): корневая операция (бюджет USDC, неизменна) → свежий сбор маршрутов у кнопки и проверка,
что победитель в одобренных границах (reselect_allowed) → проверки HL ДО свопа (режим счёта, маржа нужного dex,
isolated/плечо, чужая позиция) → своп (sol_exec: запись-до, подпись проверенных байтов, одна отправка, finalized-чек)
→ хедж только по фактическому приходу: target = floor(T·Fs/Fp, h), SELL IOC на target − S в пределах одобренной
ёмкости (излишек сверх неё — PAUSED_RISK, продажа излишка в пилоте выключена) → сверка → OPEN.
Выход — полный: продаются токены СДЕЛКИ (не кошелька), затем BUY reduceOnly на S − target(T_остаток) по факту
списания (роутер вернул часть — остаток остаётся захеджирован, сделка не CLOSED). Пыль закрывает сделку только в
обеих границах владельца (токены и USDC).
UNKNOWN Solana или HL — пауза без новых действий; исход выясняют только чтения и те же подписанные байты
(recover_deal — перед любым новым действием и в сверке). «Стоп» после свопа хедж доделывает (hedge=True), новый
своп не начинает. Добор, «продолжить» частичного выхода, «откат» и аварийные авто-действия в пилоте выключены —
честный отказ с подсказкой команды.
"""
from __future__ import annotations
import json, logging, time
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_FLOOR, localcontext
from typing import Any, Mapping
from . import hl_rules as R, instruments as I, planner, store, tconfig
from .engine import (HedgeResult, PERP_ATTEMPTS_MAX, Pause, Proposal, Refused, _views, deal_book, deal_instrument,
                     dget)
from .exposure import Exposure
from .operations import OperationController, SpotSettlement, ClipLifecycle
from .fees import NATIVE_SOL, native_cash_needed
from .keys import effective_mode, redact
from .owner import OwnerCfg, OwnerConfigError, OwnerMissing, OwnerUnsupported, SOL_HL
from .planner import PlanRefused
from .runtime import ProfileDown, SolLegs, is_sol_deal, legs_of
from .sol_exec import PresendRefused, SwapOutcome
from .spot_router import Approval, AssetRef, HedgeContext, PairParams, QuoteRequest, margin_for, reselect_allowed
from .solana.accounts import ata
from .store import ClipState, DealState, IntentStatus, OpState, PerpOrderState
from .types import ClipPlan, InstrumentSpec, Plan

log = logging.getLogger(__name__)
D = Decimal
ZERO = D(0)
BPS = D(10_000)
LIM = f"limits.{SOL_HL}."
PREVIEW_COLLECT_S = 10.0      # срок сбора котировок, если collection_deadline_ms не задан (только превью dry: live без
#                               лимита маршрут не пропустит — limit_missing)
BOOK_LEVELS = 20
_UNKNOWN_REASONS = ("dex_unknown", "perp_unknown", "position_unknown", "book_unknown")
_RISK_REASONS = ("hedge_deficit", "surplus", "reduce_only_reject", "position_mismatch", "error")


def _sv():
    from ..tg import sol_views
    return sol_views


def _lim(cfg: OwnerCfg, name: str) -> Any:
    return cfg.get(LIM + name)


def _raw(x: D, dec: int) -> int:
    return int((D(x) * D(10) ** dec).to_integral_value(ROUND_FLOOR))


def _h(raw: int | None, dec: int) -> D | None:
    return None if raw is None else D(int(raw)) / D(10) ** dec


def _step(perp) -> D:
    return R.sz_step(perp.identity().sz_decimals)


def _tokens_per_step(inst: InstrumentSpec, step: D) -> D:
    """Шаг перпа в токенах спота: h·Fp/Fs (меньше — не хеджируется, это перенос, а не голая нога)."""
    return Exposure(inst.fs, inst.fp, step).token_step


def _delta(inst: InstrumentSpec, tokens: D, short: D) -> D:
    """Δ в токенах спота: T − S·Fp/Fs (Fs — единиц базы в токене спота, Fp — в единице перпа)."""
    return Exposure(inst.fs, inst.fp, D(1)).delta(tokens, short)


def _target(inst: InstrumentSpec, tokens: D, step: D) -> D:
    """target_short(T) = floor_step(T·Fs/Fp, h) — контракты перпа (ТЗ §4)."""
    return Exposure(inst.fs, inst.fp, step).target(tokens)


def inst_mismatch(deal: Mapping, spec: Mapping, inst: InstrumentSpec) -> str | None:
    """Намерение — только по инструменту, по которому его одобрили; сравнение ТОЧНОЕ (mint base58 с регистром, S02)."""
    did = deal["id"]
    h = spec.get("inst_hash")
    if not h:
        return "план построен без отпечатка инструмента — пришлите команду заново"
    if inst.schema < 2 or inst.profile_id != SOL_HL:
        return f"сделка {did}: спецификация инструмента не связки Solana × Hyperliquid — ничего не отправлено"
    if h != inst.inst_hash():
        return f"инструмент намерения ≠ инструменту сделки {did} — ничего не отправлено"
    have = (str(deal["chain"]), str(deal["token"]), int(deal["token_dec"]), str(deal["perp_venue"]),
            str(deal["symbol"]))
    frozen = (inst.chain, inst.token, int(inst.token_dec), inst.perp_venue, inst.perp_symbol)
    if have != frozen:
        return f"сделка {did} и её инструмент расходятся — ничего не отправлено"
    for k, want in (("token", inst.token), ("symbol", inst.perp_symbol)):
        if spec.get(k) is not None and str(spec[k]) != want:
            return f"намерение: {k} ≠ сделке {did} — ничего не отправлено"
    return None


# --- применение исхода свопа (идемпотентно: чек той же подписи второй раз ничего не меняет) --------------------------
def apply_swap(con, deal_id: str, clip_id: int, op_id: str | None, reserve_raw: int, out: SwapOutcome) -> None:
    """Исход попытки → клип, операция, расходы. Вызывается внутри транзакции записи чека (sol_exec) или после
    доказанного неисполнения. Неизвестный исход сюда не приходит: он держит резерв операции и клип DEX_UNKNOWN."""
    if out.state not in ("ok", "failed", "expired", "not_sent"):
        raise ValueError(f"исход {out.state!r} не применяется")
    result = SpotSettlement(True, int(out.in_raw), int(out.out_raw)) if out.state == "ok" else SpotSettlement(False)
    with store.tx(con):
        OperationController(con).settle_spot(clip_id, result, operation_id=op_id, reserve_raw=int(reserve_raw))
        if out.fees and out.signature and not str(out.signature).startswith("sim-"):
            store.add_fee_events(con, origin_kind="sol_swap", origin_ref=str(out.signature), components=out.fees,
                                 deal_id=deal_id, operation_id=op_id, clip_id=clip_id)



def _attempts_of_deal(con, spot, deal_id: str) -> list[dict]:
    rows = spot.pending(con) if spot is not None else []
    out = []
    for r in rows:
        try:
            p = json.loads(r.get("plan_json") or "{}")
        except (TypeError, ValueError):
            p = {}
        if p.get("deal_id") == deal_id:
            out.append(dict(r, _plan=p))
    return out


def _op_to(con, op_id: str | None, *path: str, reason: str | None = None) -> bool:
    """Операция по цепочке состояний (переходы store.OP_NEXT); сбой — в журнал, не исключение (итог сделки важнее)."""
    if not op_id:
        return True
    for st in path:
        op = store.get_operation(con, op_id)
        if op is None or op["state"] == st:
            continue
        try:
            store.set_operation_state(con, op_id, st, reason=reason)
        except (store.StoreError, LookupError) as e:
            log.error("операция %s → %s: %s", op_id, st, e)
            return False
    return True


_LANDED_FINAL = ("FINALIZED_OK", "FINALIZED_ERR")
_TERMINAL = ("FINALIZED_OK", "FINALIZED_ERR", "EXPIRED_NOT_LANDED", "ROLLED_BACK", "ABANDONED_UNSIGNED")


def _settle_terminal_clip(con, did: str, c: Mapping, legs: SolLegs) -> str | None:
    """Клип DEX_SENT/DEX_UNKNOWN, чьи подписанные попытки уже в конечном состоянии журнала (резолвер их больше не
    видит): исход берётся из журнала без сети — чек finalized или доказанное неисполнение (истекла, откатилась).
    None — применено; иначе — почему нет."""
    rows = [dict(r) for r in con.execute("SELECT attempt_id, state, plan_json FROM sol_tx_attempts WHERE clip_ref=? "
                                         "ORDER BY created", (str(c["id"]),))]
    if not rows or any(r["state"] not in _TERMINAL for r in rows):
        return "исход свопа не применён"
    landed = [r for r in rows if r["state"] in _LANDED_FINAL]
    r = landed[-1] if landed else rows[-1]
    try:
        p = json.loads(r["plan_json"] or "{}")
        amount = int(p.get("amount_in_raw") or store.units(c["planned_in"]) or 0)
        out = legs.spot.resolve(con, r["attempt_id"], wait_s=0, apply=lambda o: apply_swap(
            con, did, int(c["id"]), p.get("op_id"), amount, o))
    except Exception as e:              # noqa — сверка не должна падать; упала — исход неизвестен
        return f"исход свопа не применён ({type(e).__name__}: {redact(e)[:80]})"
    return None if out.state != "unknown" else f"исход свопа не применён ({out.reason})"


def _abort_empty(con, did: str, legs: SolLegs) -> bool:
    """Сделка на паузе, в которой доказанно ничего не исполнено (своп истёк/откатился/не отправлен, заявок HL с
    исполнением нет, книга 0/0, позиция HL прочитана = 0) — снимается, как пустой ENTERING при перезапуске: иначе
    она навсегда занимает рынок, а «выход», «дохедж» и «продолжить» ей отказывают. Не трогает то, что исполняется."""
    d = store.get_deal(con, did)
    if d is None or d["state"] != DealState.PAUSED or legs.sim:
        return False
    if con.execute("SELECT 1 FROM intents WHERE deal_id=? AND status IN ('approved','running')", (did,)).fetchone():
        return False
    if con.execute("SELECT 1 FROM clips c JOIN intents i ON c.intent_id = i.id WHERE i.deal_id=? AND c.state NOT IN "
                   "('PLANNED','DEX_REVERTED')", (did,)).fetchone():
        return False
    prefix = f"fb-{did}-"
    if con.execute("SELECT 1 FROM perp_orders WHERE substr(client_id, 1, ?)=? AND state NOT IN ('NOT_PLACED', "
                   "'EXPIRED', 'REJECTED')", (len(prefix), prefix)).fetchone():
        return False
    bk = deal_book(con, did)
    if not bk.known or bk.tokens_raw != 0 or bk.short != 0 or _attempts_of_deal(con, legs.spot, did):
        return False
    try:
        pos = legs.perp.position(d["symbol"])
    except Exception:                   # noqa — позиция не прочитана: не «флэт»
        return False
    if pos is None or pos != 0:
        return False
    op = store.active_operation(con, did)
    if op is not None:
        if op["state"] in (OpState.APPROVED, OpState.RUNNING) or op["reserved_raw"] != "0":
            return False
        if not _op_to(con, op["id"], OpState.STOPPED, OpState.ABANDONED, reason="ничего не исполнено"):
            return False
    store.supersede_intents(con, did, keep="")
    store.set_deal_state(con, did, DealState.ABORTED, expect=DealState.PAUSED,
                         reason="исход выяснен: ничего не куплено, шорта нет — сделка снята")
    store.event(con, "empty_aborted", deal_id=did)
    return True


def recover_deal(con, deal: Mapping, legs: SolLegs) -> list[str]:
    """Исход прошлых отправок сделки — ДО любого нового действия (X10, R12): попытки Solana (резолвер и те же байты),
    клипы DEX_SENT без подписанной попытки (не уходили — DEX_REVERTED), заявки HL (settle по сохранённому cloid).
    Возвращает то, что осталось неизвестным (пусто — можно действовать)."""
    did = deal["id"]
    left: list[str] = []
    if legs.sim:
        return left
    open_clips: set[int] = set()
    for r in _attempts_of_deal(con, legs.spot, did):
        p = r["_plan"]
        try:
            out = legs.spot.resolve(con, r["attempt_id"], apply=lambda o, p=p: apply_swap(
                con, did, int(p["clip_id"]), p.get("op_id"), int(p["amount_in_raw"]), o))
        except Exception as e:          # noqa — сверка не должна падать; упала — исход неизвестен
            left.append(f"своп {r['attempt_id']}: {type(e).__name__}: {redact(e)[:100]}")
            continue
        if out.state == "unknown":
            left.append(f"своп {out.signature or r['attempt_id']}: {out.reason}")
            open_clips.add(int(p["clip_id"]))
            c = store.get_clip(con, int(p["clip_id"]))
            if c is not None and c["state"] == ClipState.DEX_SENT:
                store.set_clip_state(con, int(p["clip_id"]), ClipState.DEX_UNKNOWN)
    for c in con.execute("SELECT c.* FROM clips c JOIN intents i ON c.intent_id = i.id WHERE i.deal_id=? AND "
                         "c.state IN ('DEX_SENT', 'DEX_UNKNOWN')", (did,)).fetchall():
        c = dict(c)
        rows = con.execute("SELECT state FROM sol_tx_attempts WHERE clip_ref=?", (str(c["id"]),)).fetchall()
        if all(x[0] in ("VALIDATED", "ABANDONED_UNSIGNED") for x in rows):
            # подписанной попытки нет — в сеть уйти ничего не могло (запись-до): клип и резерв операции снимаются
            op = store.operation_of_intent(con, c["intent_id"])
            with store.tx(con):
                store.set_clip_state(con, c["id"], ClipState.DEX_REVERTED)
                if op is not None and int(op["reserved_raw"]) >= int(store.units(c["planned_in"]) or 0) > 0:
                    store.operation_settle(con, op["id"], released_raw=int(store.units(c["planned_in"])),
                                           executed_raw=0)
        elif c["id"] not in open_clips:
            why = _settle_terminal_clip(con, did, c, legs)
            if why:
                left.append(f"клип {c['id']}: {why}")
    perp = legs.perp
    prefix = f"fb-{did}-"
    journal = getattr(perp, "journal", None)
    sent: list[tuple[dict, dict | None]] = []
    for o in store.perp_orders_unresolved(con):
        if not str(o["client_id"]).startswith(prefix):
            continue
        cid = o["client_id"]
        row = journal.get(cid) if journal is not None else None
        from .adapters.execution import recover_not_submitted
        if recover_not_submitted(con, deal=deal, clip_id=o['clip_id'], native=perp,
                                 account=legs.account_id, client_id=cid):
            continue
        if o["state"] == PerpOrderState.INTENT and (row is None or row.get("state") == "NOT_SENT"):
            store.perp_order_result(con, cid, PerpOrderState.NOT_PLACED, err="не подписана — не отправлялась")
            continue
        sent.append((o, row))
    # позиция до заявки = −(исполненное остальными заявками сделки): одна неразрешённая — «не выставлена» доказуема
    # (позиция и fills неизменны после expiresAfter), как reconcile.resolve_orders для BSC; несколько — None
    short_known, known_ids = ZERO, set()
    pending_ids = {o["client_id"] for o, _ in sent}
    for r in con.execute("SELECT client_id, side, executed_qty, order_id FROM perp_orders WHERE substr(client_id, 1, ?)"
                         "=? AND state NOT IN (?,?,?)", (len(prefix), prefix, str(PerpOrderState.INTENT),
                                                         str(PerpOrderState.SENT), str(PerpOrderState.UNKNOWN))):
        if r[0] in pending_ids:
            continue
        q = dget(r[2]) or ZERO
        short_known += q if r[1] == "SELL" else -q
        if r[3] is not None:
            known_ids.add(int(r[3]))
    for o, row in sent:
        cid = o["client_id"]
        # окно fills — от записи попытки в журнале адаптера (его часы), иначе от отметки отправки движка
        t0 = (row or {}).get("created") or o["sent_ts"] or 0
        try:
            from .adapters.execution import settle_ioc
            f = settle_ioc(con, deal=deal, native=perp, account=legs.account_id, client_id=cid,
                           pos_before=-short_known if len(sent) == 1 else None,
                           since_ms=int(float(t0) * 1000), known_order_ids=frozenset(known_ids), wait=True)
        except Exception as e:          # noqa
            left.append(f"заявка {cid}: {type(e).__name__}")
            continue
        if f.status == "NOT_FOUND":
            if o["state"] == PerpOrderState.SENT:        # SENT → NOT_PLACED только через UNKNOWN (процесс умер до ответа)
                store.perp_order_result(con, cid, PerpOrderState.UNKNOWN, err="ответа не было")
            store.perp_order_result(con, cid, PerpOrderState.NOT_PLACED, err=f.err_text or "не выставлена")
        elif f.status in ("FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED"):
            store.record_perp_fill(con, f, err=getattr(f, "err_kind", None) if f.status == "REJECTED" else None)
        else:
            if o["state"] != PerpOrderState.UNKNOWN:
                store.perp_order_result(con, cid, PerpOrderState.UNKNOWN, err=f.err_text)
            left.append(f"заявка {cid}: исход неизвестен")
    op = store.active_operation(con, did)
    if op is not None and op["state"] in (OpState.APPROVED, OpState.RUNNING):
        # операция «идёт», а намерения, которое её ведёт, нет (процесс умер посреди шага): остановлена, цель цела
        running = con.execute("SELECT 1 FROM operation_intents oi JOIN intents i ON i.id = oi.intent_id WHERE "
                              "oi.operation_id=? AND i.status IN ('approved', 'running')", (op["id"],)).fetchone()
        if running is None:
            _op_to(con, op["id"], OpState.PAUSED_UNKNOWN if left or op["reserved_raw"] != "0" else OpState.STOPPED,
                   reason="перезапуск")
            op = store.get_operation(con, op["id"])
    if not left and op is not None and op["state"] == OpState.PAUSED_UNKNOWN and op["reserved_raw"] == "0":
        _op_to(con, op["id"], OpState.PAUSED_RISK, reason="исход выяснен — ноги сверить")
    if not left:
        _abort_empty(con, did, legs)
    return left


def check_deal(con, deal: Mapping, legs: SolLegs | None, *, resolve: bool = True, down: str | None = None):
    """Сверка сделки связки (для reconcile): книга по журналу против кошелька Solana (≥, свои токены владельца — не
    сделки) и позиции HL (=). Нельзя прочитать — None «не сверена», а не «флэт». down — почему ноги не собраны
    (сбой сборки: RPC, ключ); None — связки в процессе нет вовсе."""
    from .reconcile import DealCheck
    did = deal["id"]
    if legs is None:
        what = f"не собрана: {down}" if down else "не подключена"
        return DealCheck(did, None, f"связка Solana × Hyperliquid {what} — не сверена", deal_book(con, did))
    problems = recover_deal(con, deal, legs) if resolve else []
    bk = deal_book(con, did)
    if not problems and store.get_deal(con, did)["state"] == DealState.ABORTED:
        return DealCheck(did, True, "исход выяснен: ничего не куплено, шорта нет", bk)
    inst = deal_instrument(con, deal)
    step = delta = hedged = None
    try:
        step = _step(legs.perp)
    except Exception as e:              # noqa
        problems.append(f"мета HL: {type(e).__name__}")
    if bk.known:
        delta = _delta(inst, bk.tokens(int(deal["token_dec"])), bk.short)
        hedged = None if step is None else ZERO <= delta < _tokens_per_step(inst, step)
    if legs.sim:
        matched = True if (bk.known and not problems) else None
        detail = "; ".join(problems) or (f"симуляция: по журналу {bk.tokens(int(deal['token_dec']))} {deal['coin']}, "
                                         f"шорт {bk.short}" if bk.known else (bk.why or ""))
        return DealCheck(did, matched, detail, bk, hedged=hedged, delta=delta, step=step)
    wal = legs.spot.token_balance(inst.token, inst.token_program)
    try:
        pos = legs.perp.position(deal["symbol"])
    except Exception:                   # noqa
        pos = None
    if problems or not bk.known:
        return DealCheck(did, None, "; ".join(problems) or (bk.why or "книга неизвестна"), bk, wal, pos, hedged, delta)
    if wal is None or pos is None:
        what = " и ".join(x for x, y in (("кошелёк", wal), ("позиция HL", pos)) if y is None)
        return DealCheck(did, None, f"не прочитано: {what}", bk, wal, pos, hedged, delta)
    mism = []
    if wal < bk.tokens_raw:
        mism.append(f"в кошельке {_h(wal, int(deal['token_dec']))} < по журналу {bk.tokens(int(deal['token_dec']))} "
                    f"{deal['coin']}")
    if pos != -bk.short:
        mism.append(f"позиция HL {pos} ≠ журнал {-bk.short}")
    if resolve:                       # ручные/чужие исполнения на рынке сделки: не наш child fill — расхождение (§11)
        from . import sol_ledger
        got = sol_ledger.ingest(con, deal, legs.perp)
        foreign = sol_ledger.ledger(con, deal, fee_rate=None).foreign_fills
        if foreign:
            mism.append(f"на {deal['symbol']} исполнения не заявками сделки ({len(foreign)}) — ручные или чужие")
        elif got["errors"]:
            log.warning("учёт HL %s: %s", did, "; ".join(got["errors"])[:200])
    if mism:
        return DealCheck(did, False, "; ".join(mism), bk, wal, pos, hedged, delta)
    return DealCheck(did, True, f"кошелёк и позиция HL — как в журнале", bk, wal, pos, hedged, delta, step=step)


# --- Desk: предложения связки (поток заданий; только чтение сети) -----------------------------------------------------
class SolDesk:
    """Предпроверки и планы связки. Ничего не подписывает и не отправляет."""

    def __init__(self, desk):
        self.d = desk

    @property
    def con(self):
        return self.d.conns.get()

    def refuse(self, text: str) -> Refused:
        return Refused(_views().refused(text))

    def refuse_aborted(self, deal: Mapping) -> None:
        """Сверка только что сняла пустую сделку (recover_deal → _abort_empty): команде по ней делать нечего."""
        if store.get_deal(self.con, deal["id"])["state"] == DealState.ABORTED:
            raise self.refuse(f"сделка {deal['id']}: исход выяснен — ничего не куплено, шорта нет; сделка снята, "
                              "«вход» заново")

    def mode(self, cfg: OwnerCfg) -> str:
        return effective_mode(cfg.profile_mode(SOL_HL), self.d.keys_mode) if self.d.keys_mode else "dry"

    def legs(self, sim: bool) -> SolLegs:
        fn = getattr(self.d.legs, "for_profile", None)
        if fn is None:
            raise self.refuse("связка Solana × Hyperliquid не подключена в этом процессе")
        try:
            lg = fn(SOL_HL, sim)
        except ProfileDown as e:
            raise self.refuse(f"связка Solana × Hyperliquid недоступна: {e.reason}") from None
        if lg is None:
            raise self.refuse("живую сделку в этом режиме не трогаю: ключи связки не загружены (режим в owner.toml и "
                              "перезапуск службы)")
        return lg

    def legs_of(self, deal: Mapping) -> SolLegs:
        try:
            lg = legs_of(self.d.legs, deal)
        except ProfileDown as e:
            raise self.refuse(f"связка Solana × Hyperliquid недоступна: {e.reason}") from None
        if lg is None:
            raise self.refuse("живую сделку в этом режиме не трогаю: ключи связки не загружены")
        return lg

    def common(self, cfg: OwnerCfg, sim: bool, action: str) -> None:
        v = _views()
        con = self.con
        if store.is_paused(con):
            raise self.refuse("пауза («стоп»): новое не начинаю. Снять — «продолжить»")
        if self.d.busy() or store.busy_intents(con):
            row = con.execute("SELECT id FROM intents WHERE status IN ('approved','running') LIMIT 1").fetchone()
            raise Refused(v.busy(row[0] if row else None))
        if not sim:
            try:
                cfg.require_profile_live(SOL_HL)
            except OwnerMissing as e:
                raise Refused(v.owner_missing(e.keys, action)) from None
            except OwnerUnsupported as e:
                raise self.refuse(str(e)) from None

    def registry(self, cfg: OwnerCfg):
        fn = getattr(self.d, "registry_loader", None)
        try:
            if fn is not None:
                return fn(cfg)
            return I.load_registry(I.registry_path(cfg.get(f"profiles.{SOL_HL}.instrument_registry")))
        except I.RegistryError as e:
            raise self.refuse(f"реестр инструментов: {e}") from None

    def resolve(self, cfg: OwnerCfg, text: str, perp_dex: str | None) -> I.InstrumentSpec:
        reg = self.registry(cfg)
        allowed = tuple(cfg.get(f"profiles.{SOL_HL}.allowed_instruments") or ()) or None
        try:
            return reg.resolve(SOL_HL, text, allowed, perp_dex)
        except I.RegistryError as e:
            raise self.refuse(str(e)) from None

    def request(self, legs: SolLegs, inst: InstrumentSpec, side: str, amount_raw: int, cfg: OwnerCfg,
                purpose: str) -> QuoteRequest:
        tok = AssetRef(inst.token, inst.token_program, int(inst.token_dec), str(inst.perp_base_asset or ""))
        q = AssetRef(inst.quote_mint, inst.quote_program, int(inst.quote_dec), "USDC")
        inp, out = (q, tok) if side == "entry" else (tok, q)
        slip = _lim(cfg, "max_spot_slippage_bps")
        if slip is None or D(slip) != D(slip).to_integral_value():
            raise self.refuse(f"не задан {LIM}max_spot_slippage_bps (целые б.п.) — котировки не запрашиваю")
        dl = _lim(cfg, "collection_deadline_ms")
        span = float(dl) / 1000 if dl is not None else PREVIEW_COLLECT_S
        rent_out = legs.spot.account_rent(out.mint, out.program, inst.token_extensions if out is tok else ())
        rent_in = legs.spot.account_rent(inp.mint, inp.program, inst.token_extensions if inp is tok else ())
        w = legs.wallet
        return QuoteRequest(side=side, input=inp, output=out, amount_in_raw=int(amount_raw), wallet=w,
                            slippage_bps=int(slip), genesis_hash=inst.genesis_hash,
                            deadline_mono=legs.router.clock() + span, purpose=purpose,
                            input_account=ata(w, inp.mint, inp.program), output_account=ata(w, out.mint, out.program),
                            account_rent=((out.mint, rent_out), (inp.mint, rent_in)))

    @staticmethod
    def inflight(con, legs: SolLegs):
        def why() -> str | None:
            p = legs.spot.pending(con)
            return f"попытка {p[0]['attempt_id']} ({p[0]['state']})" if p else None
        return why

    @staticmethod
    def prices(legs: SolLegs) -> dict:
        obs = legs.native_obs()
        return {NATIVE_SOL: obs} if obs is not None else {}

    def hedge_fn(self, legs: SolLegs, inst: InstrumentSpec, cfg: OwnerCfg, side: str, *, close_qty: D | None = None,
                 available: D | None = None):
        perp = legs.perp

        def make() -> HedgeContext:
            step = _step(perp)
            lev = cfg.get("perp.hyperliquid.leverage")
            params = PairParams(fs=inst.fs, fp=inst.fp, step=step, perp_fee_rate=legs.fee_rate(),
                                min_notional=R.MIN_NOTIONAL_USD, exit_close_qty=close_qty,
                                leverage=None if lev is None else D(lev),
                                margin_reserve=_lim(cfg, "min_hl_margin_reserve_usdc"),
                                available_margin=available if side == "entry" else None)
            return HedgeContext(book=perp.book(inst.perp_symbol, BOOK_LEVELS), params=params, now_wall=self.d.clock())
        return make

    @staticmethod
    def pick(dec, sim: bool):
        """Победитель: live — только пригодный; симуляция — лучший для показа (причины live-only видны заметками)."""
        w = dec.winner if dec.winner is not None else (dec.preview_winner if sim else None)
        wr = next((x for x in dec.ranked if x.cand is w), None) if w is not None else None
        return w, wr

    @staticmethod
    def others(dec, winner) -> tuple[str, ...]:
        sv = _sv()
        out = []
        for x in dec.ranked:
            c = x.cand
            if c is winner:
                continue
            if x.eligible and x.metric is not None and winner is not None:
                wm = next((y.metric for y in dec.ranked if y.cand is winner), None)
                if wm:
                    d = (x.metric / wm - 1) * BPS if dec.side == "entry" else (1 - x.metric / wm) * BPS
                    out.append(f"{sv.PATH_LABEL.get(c.path, c.path)}: хуже на {_views().pct(d / 100, 2)}")
                    continue
            r = next((r for r in c.reasons if not r.startswith("limit_missing")), c.reasons[0] if c.reasons else "")
            out.append(f"{sv.PATH_LABEL.get(c.path, c.path)}: {sv.reason(r)}")
        for u in dec.unavailable:
            out.append(f"{sv.PATH_LABEL.get(u.path, u.path)}: {sv.reason(u.reason)}")
        return tuple(dict.fromkeys(out))

    @staticmethod
    def identity_line(inst: InstrumentSpec) -> str | None:
        """«mint 9cRC…pump ↔ para:ANSEM: подтверждено владельцем до 2026-10-13» — основание соответствия и срок."""
        who = {"reviewed_override": "подтверждено владельцем",
               "verified_source": "подтверждено источником"}.get(inst.identity_status)
        if who is None:
            return None
        return f"mint {_sv().short_mint(inst.token)} ↔ {inst.perp_symbol}: {who} до {str(inst.identity_expires_at)[:10]}"

    # --- вход ---
    def plan_entry(self, text: str, usdc: D, *, spot_policy: str = "auto", perp_dex: str | None = None,
                   cfg: OwnerCfg | None = None, sim: bool | None = None, write_checks: bool = True
                   ) -> tuple[Plan, dict]:
        cfg = cfg or self.d.cfg()
        sim = (self.mode(cfg) != "live") if sim is None else sim
        if write_checks:
            self.common(cfg, sim, "вход")
        if spot_policy != "auto":
            raise self.refuse(f"спот {spot_policy}·sol: в пилоте только sol-auto — лучший из Jupiter Build и OKX")
        if usdc is None or usdc <= 0:
            raise self.refuse("сумма входа в USDC — больше нуля")
        rec = self.resolve(cfg, text, perp_dex)
        now = self.d.clock()
        blk = I.entry_blockers(rec, now=now, allowed_mint_extensions=tuple(
            cfg.get("spot.solana.allowed_mint_extensions") or I.MINT_EXTENSIONS_V1))
        hard = [b for b in blk if b.startswith(("identity", "единицы", "spot: расширения"))]
        if hard:                      # соответствие не доказано или срок истёк — свежая цена его не заменяет (V03)
            raise self.refuse(f"{rec.display_symbol}: " + "; ".join(hard) + " — вход запрещён")
        notes = [b for b in blk if b not in hard]
        legs = self.legs(sim)
        try:
            inst = I.deal_spec(rec, account_id=legs.account_id, now=now)
        except I.RegistryError as e:
            raise self.refuse(str(e)) from None
        con = self.con
        if write_checks:              # G04: одна незакрытая сделка на scope перпа; повторный вход — добор (выключен)
            for d in store.active_deals(con):
                if d.get("perp_scope") == inst.perp_scope or (d["chain"] == inst.chain and d["token"] == inst.token):
                    st = _views().DEAL_STATE_LABEL.get(d["state"], d["state"])
                    raise self.refuse(f"по {rec.display_symbol} уже есть сделка {d['id']} ({st}) — добор в пилоте "
                                      "выключен")
        for k in ("max_clip_usdc", "max_operation_usdc", "max_total_position_usdc"):
            v = _lim(cfg, k)
            if v is None:
                notes.append(f"не задан {k}")
            elif usdc > v:
                raise self.refuse(f"{_views().num(usdc)} USDC больше лимита {k} = {_views().num(v)} — один клип пилота")
        # свежий mint против записи реестра (S03–S06): расхождение — отказ всегда; не прочитан — только live
        try:
            mm = I.mint_mismatches(rec, legs.spot.mint_value(inst.token))
            if mm:
                raise self.refuse("; ".join(mm))
        except Refused:
            raise
        except Exception as e:        # noqa
            notes.append(f"mint не прочитан ({type(e).__name__})")
        perp = legs.perp
        try:
            ref = perp.identity()
        except Exception as e:        # noqa
            raise self.refuse(f"мета Hyperliquid не прочитана: {redact(e)}") from None
        if ref.fullcoin != inst.perp_symbol or ref.dex != inst.perp_dex or ref.is_delisted or (
                inst.perp_asset_id is not None and ref.asset != inst.perp_asset_id) or (
                inst.perp_collateral is not None and inst.perp_collateral != f"token:{ref.collateral_token}"):
            raise self.refuse(f"рынок {inst.perp_symbol} на Hyperliquid не совпадает с реестром (asset {ref.asset}, "
                              f"delisted {ref.is_delisted}) — нужна новая версия записи")
        step = R.sz_step(ref.sz_decimals)
        lev = cfg.get("perp.hyperliquid.leverage")
        if lev is None:
            notes.append("не задано плечо perp.hyperliquid.leverage")
        elif int(lev) > ref.max_leverage:
            raise self.refuse(f"плечо {lev}x больше допустимого {ref.max_leverage}x на {inst.perp_symbol}")
        mv = perp.margin()
        if not mv.trade_supported:    # до свопа, а не после (H03/H09)
            notes.append(f"режим счёта HL «{mv.mode}» не поддержан первой версией: {mv.reason or ''}".strip())
        avail = mv.available
        if avail is None:
            notes.append(f"маржа HL неизвестна ({mv.source})")
        if not sim:
            pos = perp.position(inst.perp_symbol)
            if pos is None:
                raise self.refuse(f"позиция {inst.perp_symbol} не прочитана — вход не начинаю")
            if pos != 0:
                raise self.refuse(f"на счёте HL уже есть позиция {inst.perp_symbol} {pos} (не этой сделки) — вход "
                                  "запрещён")
            notes += perp.agent_refusals()
        amount_raw = _raw(usdc, int(inst.quote_dec))
        req = self.request(legs, inst, "entry", amount_raw, cfg, "entry")
        dec = legs.router.select(req, prices=self.prices(legs), block_height=legs.block_height,
                                 hedge=self.hedge_fn(legs, inst, cfg, "entry", available=avail),
                                 inflight_unknown=self.inflight(con, legs))
        winner, wr = self.pick(dec, sim)
        if winner is None:
            why = "; ".join(dec.reasons) or "нет кандидатов"
            raise self.refuse(f"нет проверенного маршрута спота ({why}): {'; '.join(self.others(dec, None)) or '—'}")
        if dec.winner is None:
            notes.append("маршрут не прошёл бы в live: " + ", ".join(dict.fromkeys(
                _sv().reason(r) for r in winner.reasons)))
        book = perp.book(inst.perp_symbol, BOOK_LEVELS)
        if not book.bids or not book.asks:
            raise self.refuse(f"стакан {inst.perp_symbol} пуст — объём шорта неизвестен")
        slip = _lim(cfg, "max_hedge_slippage_bps")
        cap = perp.quantize_px(inst.perp_symbol, book.bids[0][0] * (1 - D(slip) / BPS), "SELL") if slip is not None \
            else book.bids[0][0]
        if slip is None:
            notes.append("не задан max_hedge_slippage_bps")
        spot_px = D(amount_raw) / D(winner.expected_out_raw) * D(10) ** (int(inst.token_dec) - int(inst.quote_dec))
        st_, su_ = _lim(cfg, "max_surplus_tokens"), _lim(cfg, "max_surplus_usdc")
        surplus = ZERO
        if st_ is None or su_ is None:
            notes.append("не задан предел излишка (max_surplus_tokens / max_surplus_usdc)")
        else:
            surplus = min(D(st_), D(su_) / spot_px)
        try:
            rc = planner.route_clip(side="entry", amount_in_raw=amount_raw, dec_in=int(inst.quote_dec),
                                    dec_out=int(inst.token_dec), expected_out_raw=int(winner.expected_out_raw),
                                    min_out_raw=winner.effective_min_out,
                                    external=wr.valuation.total if wr and wr.valuation else None, book=book, step=step,
                                    fs=inst.fs, fp=inst.fp, fee_rate=legs.fee_rate(), cap_px=cap,
                                    surplus_tokens=surplus)
        except PlanRefused as e:
            raise self.refuse(str(e)) from None
        v = _views()
        mb = _lim(cfg, "min_entry_basis_all_in_bps")
        if rc.basis_all_in_bps is None:
            notes.append("курсовой с издержками неизвестен (расход или ставка HL не известны)")
        elif mb is not None and rc.basis_all_in_bps < D(mb):
            notes.append(f"курсовой с издержками {v.pct(rc.basis_all_in_bps / 100, 2, sign=True)} ниже порога "
                         f"{v.pct(D(mb) / 100, 2, sign=True)}")
        fx = _lim(cfg, "max_external_fees_usdc_per_operation")
        if rc.external is not None and fx is not None and rc.external > D(fx):
            notes.append(f"расходы спота {v.num(rc.external, 4)} USDC больше лимита операции {v.num(fx)}")
        dt, du = _lim(cfg, "max_delta_tokens"), _lim(cfg, "max_delta_usdc")
        if dt is None or du is None:
            notes.append("не задан предел голой дельты (max_delta_tokens / max_delta_usdc)")
        elif rc.residual_tokens > D(dt) or rc.residual_tokens * spot_px > D(du):
            notes.append(f"остаток без хеджа {v.tok(rc.residual_tokens)} больше предела дельты")
        if rc.qty_min is not None and rc.qty_min * cap < R.MIN_NOTIONAL_USD:
            raise self.refuse(f"при минимальном выходе шорт {rc.qty_min} меньше минимального ордера HL "
                              f"{R.MIN_NOTIONAL_USD} $ — не увеличиваю шорт ради минимума (U17)")
        reserve = _lim(cfg, "min_hl_margin_reserve_usdc")
        margin_need = None
        if lev is not None and avail is not None and reserve is not None:
            need = margin_for(rc.capacity, book.asks[0][0], D(lev), legs.fee_rate()) + D(reserve)
            margin_need = need
            if need > avail:
                notes.append(f"маржи HL {v.num(avail)} USDC < нужно {v.num(need)} (шорт с излишком + резерв)")
        bal = legs.spot.token_balance(inst.quote_mint, inst.quote_program)
        if bal is None:
            notes.append("баланс USDC не прочитан")
        elif bal < amount_raw:
            notes.append(f"USDC в кошельке {v.num(_h(bal, int(inst.quote_dec)))} — меньше {v.num(usdc)}")
        need_lam = native_cash_needed(winner.fees, legs.wallet, _lim(cfg, "min_sol_reserve_lamports"))
        nat = legs.spot.native_balance()
        if need_lam is None:
            notes.append("запас SOL не проверить (резерв владельца или расход сети неизвестны)")
        elif nat is None:
            notes.append("баланс SOL не прочитан")
        elif nat < need_lam:
            notes.append(f"SOL {v.num(_h(nat, 9), 4)} — меньше расходов с резервом {v.num(_h(need_lam, 9), 4)}")
        if notes and not sim:
            raise self.refuse("; ".join(dict.fromkeys(notes)))
        fund = None
        try:
            fund = perp.funding(inst.perp_symbol)[1]
        except Exception:             # noqa — фандинг — справка плана
            pass
        est = {**rc.as_est(), "path": winner.path, "provider": winner.provider, "request_hash": req.request_hash,
               "expected_out_raw": winner.expected_out_raw, "min_out_raw": winner.effective_min_out,
               "metric": wr.metric if wr else None, "conservative": wr.conservative if wr else None,
               "decision": dec.status, "n": 1, "clip_usd": usdc, "units_per_contract": inst.m,
               "total_usd": (rc.external or ZERO) + (rc.perp_fee or ZERO), "tokens": rc.tokens}
        plan = Plan(deal_id="", kind="entry", coin=rec.display_symbol, spot="auto·solana",
                    perp=f"hyperliquid·{inst.perp_dex}", symbol=inst.perp_symbol, leg_usd=usdc,
                    clips=[ClipPlan(seq=1, dex_in_units=amount_raw, children=[list(c) for c in rc.children])], est=est,
                    inputs={"inst_hash": inst.inst_hash(), "filters": {"step": step}, "book_top": {
                        "bid": book.bids[0][0], "ask": book.asks[0][0]}, "fee_taker": legs.fee_rate(),
                        "funding_h": fund},
                    missing_owner_keys=list(cfg.profile_live_missing(SOL_HL)), expires=now + tconfig.PLAN_TTL_S)
        ctx = dict(sim=sim, cfg=cfg, inst=inst, rec=rec, legs=legs, dec=dec, winner=winner, wr=wr, rc=rc, req=req,
                   notes=notes, funding_h=fund, amount_raw=amount_raw, cap=cap, margin_need=margin_need,
                   margin_avail=avail)
        return plan, ctx

    def plan_view(self, iid: str, plan: Plan, ctx: dict, deal_id: str | None = None):
        sv, rc, inst = _sv(), ctx["rc"], ctx["inst"]
        entry = plan.kind == "entry"
        f = ctx.get("funding_h")
        return sv.SolPlanView(
            intent_id=iid, kind=plan.kind, coin=plan.coin, fullcoin=inst.perp_symbol, deal_id=deal_id,
            usdc=rc.stable, usdc_min=rc.stable_min, tokens=rc.tokens, tokens_min=rc.tokens_min, perp_qty=rc.qty,
            perp_px=rc.perp_vwap, path=ctx["winner"].path, others=self.others(ctx["dec"], ctx["winner"]),
            basis_bps=rc.basis_all_in_bps, basis_gross_bps=rc.basis_gross_bps, spot_fee_usd=rc.external,
            perp_fee_usd=rc.perp_fee, funding_pct_h=(f * 100) if (f is not None and entry) else None,
            leverage=ctx["cfg"].get("perp.hyperliquid.leverage") if entry else None,
            identity=self.identity_line(inst) if entry else None, notes=tuple(dict.fromkeys(ctx["notes"])),
            missing=tuple(plan.missing_owner_keys) if ctx["sim"] else (), sim=ctx["sim"],
            margin_need=ctx.get("margin_need") if entry else None, margin_avail=ctx.get("margin_avail") if entry else None)

    def propose_entry(self, cmd, chat: int | None) -> Proposal:
        """cmd — tg.parse.ProfileEntry (coin, spot_policy, perp_dex, usdc)."""
        plan, ctx = self.plan_entry(cmd.coin, cmd.usdc, spot_policy=cmd.spot_policy, perp_dex=cmd.perp_dex)
        con = self.con
        cfg, sim, inst, rec, winner, rc = ctx["cfg"], ctx["sim"], ctx["inst"], ctx["rec"], ctx["winner"], ctx["rc"]
        wr = ctx["wr"]
        # прежние планы входа на тот же рынок счёта — неактуальны: их кнопки не исполняются, черновики сняты
        superseded: list[str] = []
        for (old,) in con.execute("SELECT id FROM deals WHERE state=? AND perp_scope=?",
                                  (str(DealState.DRAFT), inst.perp_scope)).fetchall():
            superseded += store.supersede_intents(con, old, keep="")
            try:
                store.set_deal_state(con, old, DealState.ABORTED, expect=DealState.DRAFT, reason="заменён новым планом")
            except store.StoreError as e:
                log.warning("черновик %s не снят: %s", old, e)
        # время сделки — те же часы, что у исполнения и журналов HL: окно учёта fills/фандинга сделки от него
        did = store.create_deal(con, coin=rec.display_symbol, chain=inst.chain, token=inst.token,
                                token_dec=int(inst.token_dec), perp_venue=inst.perp_venue, symbol=inst.perp_symbol,
                                leg_usd=cmd.usdc, owner_json=cfg.frozen_json(), sim=sim, inst=inst, now=self.d.clock())
        plan.deal_id = did
        fx = _lim(cfg, "max_external_fees_usdc_per_operation")
        # одобренные границы (R13): тот же запрос, политика best, не хуже одобренного минимума и стоимости
        approval = dict(request_hash=ctx["req"].request_hash, side="entry", policy="best", provider_group=winner.group,
                        min_out_raw=int(winner.effective_min_out or 0),
                        max_cost_per_token=str(wr.conservative if (wr and wr.conservative is not None) else
                                               (wr.metric if wr else "")) or None)
        op_id = store.create_operation(con, deal_id=did, profile_id=SOL_HL, inst_hash=inst.inst_hash(),
                                       mode="dry" if sim else "live", side="entry", target_kind="stable_raw_budget",
                                       target_asset=inst.quote_mint, target_decimals=int(inst.quote_dec),
                                       target_raw=ctx["amount_raw"],
                                       fee_cap_raw=None if fx is None else _raw(D(fx), int(inst.quote_dec)),
                                       bounds=approval)
        store.append_route_rounds(con, ctx["dec"].records(op_id, 0))
        spec = {"kind": "entry", "profile": SOL_HL, "coin": rec.display_symbol, "usd": cmd.usdc,
                "amount_raw": ctx["amount_raw"], "token": inst.token, "token_dec": int(inst.token_dec),
                "symbol": inst.perp_symbol, "sim": sim, "owner": cfg.frozen_json(), "funding_h": ctx["funding_h"],
                "period_h": 1, "instrument": inst.as_dict(), "inst_hash": inst.inst_hash(),
                "instrument_id": rec.instrument_id, "instrument_version": rec.version, "operation_id": op_id,
                "approval": approval, "hedge_capacity": rc.capacity, "spot": "auto·solana",
                "perp": f"hyperliquid·{inst.perp_dex}"}
        iid, nonce = store.create_intent(con, deal_id=did, kind="entry", spec=spec, plan=plan, chat=chat)
        store.link_intent(con, op_id, iid)
        store.event(con, "proposed", deal_id=did, intent_id=iid, total_usd=plan.est.get("total_usd"), n=1, sim=sim,
                    path=winner.path, operation_id=op_id)
        return Proposal(iid, nonce, did, "entry", _sv().plan(self.plan_view(iid, plan, ctx, did)), plan,
                        superseded=tuple(superseded))

    # --- выход ---
    def plan_exit(self, deal: Mapping, *, cfg: OwnerCfg | None = None, write_checks: bool = True
                  ) -> tuple[Plan, dict]:
        cfg = cfg or self.d.cfg()
        sim = bool(deal["sim"])
        if write_checks:
            self.common(cfg, sim, "выход")
        v = _views()
        if deal["state"] not in (DealState.OPEN, DealState.PAUSED):
            raise self.refuse(f"сделка {deal['id']} {v.DEAL_STATE_LABEL.get(deal['state'], deal['state'])} — выход не "
                              "начинаю")
        con = self.con
        inst = deal_instrument(con, deal)
        if inst.schema < 2 or inst_mismatch(deal, {"inst_hash": inst.inst_hash()}, inst):
            raise self.refuse(f"сделка {deal['id']}: спецификация инструмента не читается или расходится — не торгую")
        try:                          # отозванное соответствие/единицы — только отдельная политика (G06)
            rec = self.registry(cfg).get(inst.instrument_id, inst.instrument_version)
            pb = I.protective_blockers(rec)
            if pb:
                raise self.refuse("; ".join(pb))
        except Refused:
            raise
        except Exception as e:        # noqa — реестр не прочитан: выход по замороженной спецификации сделки
            log.warning("реестр для выхода %s: %s", deal["id"], redact(e))
        legs = self.legs_of(deal)
        left = recover_deal(con, deal, legs)       # сначала выяснить прошлые отправки (только чтения, те же байты)
        if left:
            raise self.refuse(f"исход прошлой отправки неизвестен: {'; '.join(left)} — сначала «позиции»")
        self.refuse_aborted(deal)
        bk = deal_book(con, deal["id"])
        if not bk.known:
            raise self.refuse(f"книга сделки неизвестна ({bk.why}) — сначала «позиции»")
        dec_t = int(deal["token_dec"])
        perp = legs.perp
        step = _step(perp)
        T, S = bk.tokens(dec_t), bk.short
        if bk.tokens_raw <= 0:
            raise self.refuse(f"спота в сделке нет (шорт {S}) — «дохедж {deal['id']}» откупит шорт")
        d = _delta(inst, T, S)
        if not (ZERO <= d < _tokens_per_step(inst, step)):
            raise self.refuse(f"ноги не ровно: без хеджа {v.tok(d, True)} {deal['coin']} — сначала "
                              f"«дохедж {deal['id']}»")
        if not sim:
            wal = legs.spot.token_balance(inst.token, inst.token_program)
            if wal is None:
                raise self.refuse("баланс токена в кошельке не прочитан — выход не начинаю")
            if wal < bk.tokens_raw:   # U10: не продаю min(кошелёк, книга) и не объявляю книгу закрытой
                raise self.refuse(f"в кошельке {v.tok(_h(wal, dec_t))} меньше, чем по журналу {v.tok(T)} — сначала "
                                  "«позиции»")
            pos = perp.position(inst.perp_symbol)
            if pos is None:
                raise self.refuse(f"позиция {inst.perp_symbol} не прочитана — выход не начинаю")
            if pos != -S:             # U18: формула выхода — только к согласованному S
                raise self.refuse(f"позиция HL {pos} ≠ журнал сделки {-S} — сначала «позиции»")
            bad = perp.agent_refusals()
            if bad:                   # продажа спота без откупа шорта оставила бы голый шорт
                raise self.refuse("; ".join(bad) + " — выход не начинаю")
        units = int(bk.tokens_raw)
        req = self.request(legs, inst, "exit", units, cfg, "exit")
        dec = legs.router.select(req, prices=self.prices(legs), block_height=legs.block_height,
                                 hedge=self.hedge_fn(legs, inst, cfg, "exit", close_qty=S),
                                 inflight_unknown=self.inflight(con, legs))
        winner, wr = self.pick(dec, sim)
        if winner is None:
            raise self.refuse(f"нет проверенного маршрута продажи ({'; '.join(dec.reasons) or '—'}): "
                              f"{'; '.join(self.others(dec, None)) or '—'}")
        notes = []
        if dec.winner is None:
            notes.append("маршрут не прошёл бы в live: " + ", ".join(dict.fromkeys(
                _sv().reason(r) for r in winner.reasons)))
        book = perp.book(inst.perp_symbol, BOOK_LEVELS)
        if not book.asks:
            raise self.refuse(f"аски {inst.perp_symbol} пусты — откуп шорта неизвестен")
        slip = _lim(cfg, "max_hedge_slippage_bps")
        cap = perp.quantize_px(inst.perp_symbol, book.asks[0][0] * (1 + D(slip) / BPS), "BUY") if slip is not None \
            else book.asks[0][0]
        try:
            rc = planner.route_clip(side="exit", amount_in_raw=units, dec_in=dec_t, dec_out=int(inst.quote_dec),
                                    expected_out_raw=int(winner.expected_out_raw), min_out_raw=winner.effective_min_out,
                                    external=wr.valuation.total if wr and wr.valuation else None, book=book, step=step,
                                    fs=inst.fs, fp=inst.fp, fee_rate=legs.fee_rate(), cap_px=cap,
                                    close_qty=S if S > 0 else None)
        except PlanRefused as e:
            raise self.refuse(str(e)) from None
        fx = _lim(cfg, "max_external_fees_usdc_per_operation")
        if rc.external is not None and fx is not None and rc.external > D(fx):
            notes.append(f"расходы спота {v.num(rc.external, 4)} USDC больше лимита операции {v.num(fx)}")
        if notes and not sim:
            raise self.refuse("; ".join(notes))
        est = {**rc.as_est(), "path": winner.path, "provider": winner.provider, "request_hash": req.request_hash,
               "expected_out_raw": winner.expected_out_raw, "min_out_raw": winner.effective_min_out,
               "metric": wr.metric if wr else None, "decision": dec.status, "n": 1,
               "total_usd": (rc.external or ZERO) + (rc.perp_fee or ZERO), "contracts": S,
               "units_per_contract": inst.m}
        plan = Plan(deal_id=deal["id"], kind="exit", coin=deal["coin"], spot="auto·solana",
                    perp=f"hyperliquid·{inst.perp_dex}", symbol=inst.perp_symbol, leg_usd=rc.stable,
                    clips=[ClipPlan(seq=1, dex_in_units=units, children=[list(c) for c in rc.children])], est=est,
                    inputs={"inst_hash": inst.inst_hash(), "filters": {"step": step}, "book_top": {
                        "bid": book.bids[0][0] if book.bids else None, "ask": book.asks[0][0]},
                        "fee_taker": legs.fee_rate()},
                    missing_owner_keys=list(cfg.profile_live_missing(SOL_HL)),
                    expires=self.d.clock() + tconfig.PLAN_TTL_S)
        ctx = dict(sim=sim, cfg=cfg, inst=inst, legs=legs, dec=dec, winner=winner, wr=wr, rc=rc, req=req, notes=notes,
                   units=units, book=bk)
        return plan, ctx

    def propose_exit(self, deal: Mapping, usd: D | None, perp_only: bool, chat: int | None,
                     *, operation_id: str | None = None) -> Proposal:
        if perp_only:
            raise self.refuse(f"«выход перп» в пилоте выключен — «выход {deal['id']}» продаёт спот сделки и закрывает "
                              "шорт")
        if usd is not None:
            raise self.refuse(f"частичный выход в пилоте выключен — только «выход {deal['id']}» целиком")
        plan, ctx = self.plan_exit(deal)
        con = self.con
        cfg, inst, winner, wr, sim = ctx["cfg"], ctx["inst"], ctx["winner"], ctx["wr"], ctx["sim"]
        approval = dict(request_hash=ctx["req"].request_hash, side="exit", policy="best", provider_group=winner.group,
                        min_out_raw=int(winner.effective_min_out or 0),
                        min_net_usdc=str(wr.conservative if (wr and wr.conservative is not None) else
                                         (wr.metric if wr else "")) or None)
        spec = {"kind": "exit", "profile": SOL_HL, "coin": deal["coin"], "usd": None, "units": ctx["units"],
                "all": True, "perp_only": False, "token": inst.token, "token_dec": int(inst.token_dec),
                "symbol": inst.perp_symbol,
                "sim": sim, "owner": cfg.frozen_json(), "period_h": 1, "instrument": inst.as_dict(),
                "inst_hash": inst.inst_hash(), "approval": approval, "root": None,
                "root_units": ctx["units"]}
        from .operation_roots import propose
        if operation_id is not None:
            previous = store.get_operation(con, operation_id)
            if previous is None:
                raise self.refuse("исходная операция выхода не найдена")
            spec["root_units"] = int(previous["target_raw"])
            spec["resume"] = True
        try:
            iid, nonce = propose(con, deal=deal, kind="exit", spec=spec, plan=plan,
                                 profile_id=SOL_HL, chat=chat, operation_id=operation_id)
        except store.StoreError as exc:
            raise self.refuse(f"продолжение исходной цели выхода: {exc}") from None
        op_id = store.operation_of_intent(con, iid)["id"]
        store.append_route_rounds(con, ctx["dec"].records(op_id, 0))
        superseded = store.supersede_intents(con, deal["id"], keep=iid)     # старые кнопки этой сделки — не исполняются
        store.event(con, "proposed", deal_id=deal["id"], intent_id=iid, total_usd=plan.est.get("total_usd"), sim=sim,
                    path=winner.path, operation_id=op_id)
        return Proposal(iid, nonce, deal["id"], "exit", _sv().plan(self.plan_view(iid, plan, ctx, deal["id"])), plan,
                        superseded=tuple(superseded))

    # --- дохедж / откат / продолжить ---
    def propose_fix(self, kind: str, deal: Mapping, chat: int | None) -> Proposal:
        v = _views()
        if kind != "rehedge":
            raise self.refuse(f"«откат» в пилоте выключен — «выход {deal['id']}» продаёт спот сделки и закрывает шорт")
        cfg = self.d.cfg()
        sim = bool(deal["sim"])
        self.common(cfg, sim, "дохедж")
        if deal["state"] not in (DealState.PAUSED, DealState.OPEN):
            raise self.refuse(f"сделка {deal['id']} {v.DEAL_STATE_LABEL.get(deal['state'], deal['state'])}")
        con = self.con
        legs = self.legs_of(deal)
        left = recover_deal(con, deal, legs)
        if left:
            raise self.refuse(f"исход прошлой отправки неизвестен: {'; '.join(left)} — сначала «позиции»")
        self.refuse_aborted(deal)
        inst = deal_instrument(con, deal)
        bk = deal_book(con, deal["id"])
        if not bk.known:
            raise self.refuse(f"книга сделки неизвестна ({bk.why}) — сначала «позиции»")
        step = _step(legs.perp)
        T, S = bk.tokens(int(deal["token_dec"])), bk.short
        target = _target(inst, T, step)
        d = _delta(inst, T, S)
        if target > S:
            if I.protective_blockers(self.registry(cfg).get(inst.instrument_id, inst.instrument_version)):
                raise self.refuse("соответствие mint ↔ перп отозвано — новый шорт не открываю (G06)")
            side, qty = "SELL", target - S
        elif target < S:
            side, qty = "BUY", S - target
        else:
            raise self.refuse(f"ноги ровно (дельта {v.tok(d, True)} меньше шага) — дохеджировать нечего")
        if not sim:
            pos = legs.perp.position(inst.perp_symbol)
            if pos is None or pos != -S:
                raise self.refuse(f"позиция HL {pos} ≠ журнал сделки {-S} — сначала «позиции»")
        spec = {"kind": "rehedge", "profile": SOL_HL, "coin": deal["coin"], "token": inst.token,
                "token_dec": int(deal["token_dec"]), "symbol": inst.perp_symbol, "sim": sim, "owner": cfg.frozen_json(),
                "instrument": inst.as_dict(), "inst_hash": inst.inst_hash(), "side": side, "qty": qty}
        px = None
        try:
            px = planner.mid(legs.perp.book(inst.perp_symbol, 5))
        except Exception:             # noqa
            pass
        plan = Plan(deal_id=deal["id"], kind="rehedge", coin=deal["coin"], spot="auto·solana",
                    perp=f"hyperliquid·{inst.perp_dex}", symbol=inst.perp_symbol,
                    leg_usd=(abs(d) * px * inst.fs / inst.fp) if px else ZERO, clips=[], est={"delta": d},
                    inputs={"inst_hash": inst.inst_hash()}, missing_owner_keys=list(cfg.profile_live_missing(SOL_HL)),
                    expires=self.d.clock() + tconfig.PLAN_TTL_S)
        iid, nonce = store.create_intent(con, deal_id=deal["id"], kind="rehedge", spec=spec, plan=plan, chat=chat)
        superseded = store.supersede_intents(con, deal["id"], keep=iid)
        text = v.fix_plan(v.FixPlanView(intent_id=iid, kind="rehedge", coin=deal["coin"], deal_id=deal["id"], delta=d,
                                        qty=qty, side=side, usd=plan.leg_usd, perp_venue="hyperliquid", step=step,
                                        sim=sim, m=inst.m))
        return Proposal(iid, nonce, deal["id"], "rehedge", text, plan, superseded=tuple(superseded))

    def propose_resume(self, deal: Mapping, chat: int | None) -> Proposal | str:
        v = _views()
        con = self.con
        if deal["state"] in (DealState.ABORTED, DealState.CLOSED):
            raise self.refuse(f"сделка {deal['id']} {v.DEAL_STATE_LABEL.get(deal['state'], deal['state'])} — "
                              "продолжать нечего")
        if deal["state"] == DealState.HALTED_MISMATCH:
            try:
                legs = legs_of(self.d.legs, deal)
            except ProfileDown:
                legs = None
            chk = check_deal(con, deal, legs)
            if chk.matched:
                store.set_deal_state(con, deal["id"], DealState.PAUSED, reason="сверено владельцем")
                return v.resume_checked(deal["id"], sim=bool(deal["sim"]))
            return v.resume_mismatch(deal["id"], chk.detail, sim=bool(deal["sim"]))
        last = con.execute("SELECT * FROM intents WHERE deal_id=? AND kind IN ('entry','exit') "
                           "AND status NOT IN ('proposed','rejected','expired') ORDER BY created DESC "
                           "LIMIT 1", (deal["id"],)).fetchone()
        if last is None:
            raise self.refuse("у сделки нет входа — продолжать нечего")
        if last["status"] in (IntentStatus.PARTIAL, IntentStatus.INTERRUPTED, IntentStatus.FAILED):
            if last["kind"] == "entry":
                raise self.refuse(f"добор в пилоте выключен — «выход {deal['id']}» закрывает сделку")
            op = store.operation_of_intent(con, last["id"])
            if op is None:
                raise self.refuse("у предыдущего выхода нет корневой операции — нужна проверка журнала")
            return self.propose_exit(deal, None, False, chat, operation_id=op["id"])
        raise self.refuse(f"последнее намерение {last['id']} — {last['status']}: продолжать нечего")


# --- исполнитель связки -------------------------------------------------------------------------------------------
@dataclass
class SolRun:
    it: dict
    deal: dict
    kind: str
    spec: dict
    plan: Plan
    legs: SolLegs
    cfg: OwnerCfg
    inst: InstrumentSpec
    step: D
    op_id: str | None
    started: float
    seq: int = 0
    swap: SwapOutcome | None = None
    perp_filled: D = ZERO
    perp_quote: D = ZERO
    t_swap: float | None = None       # начало свопа: от него до конца хеджа нога голая (лимит max_unhedged_ms)

    @property
    def iid(self) -> str:
        return self.it["id"]

    @property
    def did(self) -> str:
        return self.deal["id"]

    @property
    def dec(self) -> int:
        return int(self.deal["token_dec"])

    @property
    def symbol(self) -> str:
        return self.inst.perp_symbol

    @property
    def letter(self) -> str:
        return {"entry": "e", "exit": "x", "rehedge": "h"}[self.kind]


class SolEngine:
    """Исполнение намерений связки. Единственный поток отправки — поток Engine; здесь — только шаги."""

    def __init__(self, engine):
        self.e = engine
        self.desk = SolDesk(engine.desk)

    @property
    def con(self):
        return self.e.conns.get()

    # --- вход в исполнение ---
    def execute(self, it: dict, deal: dict, spec: dict) -> None:
        con, iid = self.con, it["id"]
        inst = deal_instrument(con, deal)
        bad = inst_mismatch(deal, spec, inst)
        if bad:
            store.event(con, "inst_mismatch", deal_id=deal["id"], intent_id=iid, why=bad)
            self._fail_op(it)
            self.e._fail(iid, bad)
            if deal["state"] == DealState.DRAFT:
                store.set_deal_state(con, deal["id"], DealState.ABORTED, expect=DealState.DRAFT, reason="не начата")
            return
        try:
            legs = legs_of(self.e.legs, deal)
        except ProfileDown as e:
            self._fail_op(it)
            return self.e._fail(iid, f"связка Solana × Hyperliquid недоступна: {e.reason} — ничего не отправлено")
        if legs is None or (not legs.sim and not legs.can_send):
            self._fail_op(it)
            return self.e._fail(iid, "живую сделку в этом режиме не двигаю: ключи связки не загружены")
        cfg = OwnerCfg.from_frozen(spec["owner"])
        self.e.holder.set(cfg)
        left = recover_deal(con, deal, legs)
        if left:                      # R12/X10: поверх неизвестного исхода — ничего нового
            self._fail_op(it)
            return self.e._fail(iid, f"исход прошлой отправки неизвестен: {'; '.join(left)} — ничего не отправлено, "
                                     "сначала «позиции»")
        deal = store.get_deal(con, deal["id"])
        try:
            step = _step(legs.perp)
        except Exception as e:        # noqa — ничего не отправлено
            self._fail_op(it)
            return self.e._fail(iid, f"мета {inst.perp_symbol} не прочитана: {redact(e)}")
        op = store.operation_of_intent(con, iid)
        if it["kind"] in ("entry", "exit"):
            # кнопка привязана к корневой операции: та же операция, те же одобренные границы (хеш), ещё не начата
            want = spec.get("operation_id")
            if op is None or op["id"] != want or op["state"] != OpState.APPROVED or \
                    op["bounds_hash"] != store._json_hash(spec.get("approval")):
                store.event(con, "stale_button", deal_id=deal["id"], intent_id=iid, operation_id=want,
                            op_state=op["state"] if op else None)
                self.e._fail(iid, f"кнопка от старого плана (операция {want} уже не та) — ничего не отправлено, "
                                  "пришлите команду заново")
                if deal["state"] == DealState.DRAFT:
                    store.set_deal_state(con, deal["id"], DealState.ABORTED, expect=DealState.DRAFT,
                                         reason="не начата")
                return
        store.set_intent_status(con, iid, IntentStatus.RUNNING, expect=IntentStatus.APPROVED)
        run = SolRun(it=it, deal=deal, kind=it["kind"], spec=spec, plan=_plan(it), legs=legs, cfg=cfg, inst=inst,
                     step=step, op_id=op["id"] if op else None, started=self.e.clock())
        store.event(con, "start", deal_id=run.did, intent_id=iid, intent_kind=run.kind, sim=legs.sim)
        try:
            if run.kind == "entry":
                self._entry(run)
            elif run.kind == "exit":
                self._exit(run)
            elif run.kind == "rehedge":
                self._rehedge(run)
            else:
                raise Pause("changed", f"«{run.kind}» в пилоте выключен")
        except Pause as p:
            self._paused(run, p)
        except Exception as e:        # noqa — неожиданное: пауза и сверка
            log.exception("исполнение %s", iid)
            self._paused(run, Pause("error", f"сбой исполнителя: {type(e).__name__}: {redact(e)}"))

    def _fail_op(self, it: dict) -> None:
        op = store.operation_of_intent(self.con, it["id"])
        if op is not None and op["state"] in (OpState.PROPOSED, OpState.APPROVED):
            if int(op["reserved_raw"]):
                _op_to(self.con, op["id"], OpState.APPROVED, OpState.PAUSED_UNKNOWN, reason="исход не выяснен")
            else:
                _op_to(self.con, op["id"], OpState.APPROVED, OpState.STOPPED, reason="не начата")
                if not int(op["confirmed_raw"]):
                    _op_to(self.con, op["id"], OpState.ABANDONED, reason="не начата")

    # --- ворота ---
    def guard(self, run: SolRun, *, entry: bool = False) -> None:
        con = self.con
        if store.is_paused(con) or self.e.pause_evt.is_set():
            raise Pause("stop", "стоп владельца: новое не начинаю")
        if self.e.drain_evt.is_set():
            raise Pause("drain", "переключение версии: новое не начинаю")
        if self.e.term.is_set():
            raise Pause("terminate", "служба останавливается: новое не начинаю")
        try:
            cfg = self.e.owner_loader()
        except OwnerConfigError as e:
            raise Pause("owner", f"owner.toml не прочитан: {e}") from None
        if not run.legs.sim:
            m = effective_mode(cfg.profile_mode(SOL_HL), self.e.keys_mode or "dry")
            if m != "live":
                raise Pause("mode", f"режим {m}: отправки запрещены")
            try:
                cfg.require_profile_live(SOL_HL)
            except (OwnerMissing, OwnerUnsupported) as e:
                raise Pause("owner_missing", str(e)) from None
        if entry:
            try:
                rec = self.desk.registry(cfg).get(run.inst.instrument_id, run.inst.instrument_version)
                hard = [b for b in I.entry_blockers(rec, now=self.e.clock())
                        if b.startswith(("identity", "единицы", "spot: расширения"))]
            except (Refused, I.RegistryError) as e:
                hard = [f"реестр не прочитан: {getattr(e, 'html', e)}"]
            if hard:
                raise Pause("identity", "; ".join(hard) + " — вход не начинаю")
            usd = dget(run.deal["leg_usd"])
            for k in ("max_clip_usdc", "max_operation_usdc", "max_total_position_usdc"):
                v = cfg.get(LIM + k)
                if v is not None and usd is not None and usd > v:
                    raise Pause("limit", f"сумма {usd} USDC больше нового лимита {k} = {v}")
            f0 = dget(run.spec.get("funding_h"))
            if f0 is not None and f0 > 0:
                try:
                    rate = run.legs.perp.funding(run.symbol)[1]
                except Exception as e:  # noqa
                    raise Pause("funding", f"фандинг не прочитан: {redact(e)}") from None
                if rate <= 0:
                    raise Pause("funding_sign", "фандинг сменил знак — вход не начинаю")

    # --- шаги ---
    def _deal_to(self, run: SolRun, new: str) -> None:
        con = self.con
        d = store.get_deal(con, run.did)
        if d["state"] == new:
            return
        try:
            if d["state"] == DealState.OPEN and new == DealState.ENTERING:
                store.set_deal_state(con, run.did, DealState.PAUSED, reason="продолжение входа")
            store.set_deal_state(con, run.did, new)
        except store.StoreBusy as e:  # G04: второй владелец той же net-позиции HL не создаётся
            raise Pause("busy", f"рынок уже занят другой активной сделкой: {e}") from None
        except store.BadTransition as e:
            raise Pause("state", f"сделка {run.did} в состоянии {d['state']}: {e}") from None

    def _op_start(self, run: SolRun) -> None:
        con = self.con
        if not run.op_id:
            raise Pause("state", "намерение без корневой операции — пришлите команду заново")
        for o in con.execute("SELECT id, state FROM operations WHERE deal_id=? AND id<>? AND state IN ('PARTIAL', "
                             "'STOPPED', 'PAUSED_RISK')", (run.did, run.op_id)).fetchall():
            _op_to(con, o[0], OpState.ABANDONED, reason=f"новая операция {run.op_id}")
        if not _op_to(con, run.op_id, OpState.RUNNING):
            raise Pause("state", f"операция {run.op_id} не запускается (другая активна или исход неизвестен)")

    @staticmethod
    def _agent_gate(run: SolRun, what: str) -> None:
        from .adapters.hl_preflight import agent_gate, PreflightRefused
        try:
            agent_gate(run.legs.perp, sim=run.legs.sim, what=what)
        except PreflightRefused as e:
            raise Pause(e.code, str(e)) from None

    def _hl_preflight(self, run: SolRun) -> None:
        from .adapters.hl_preflight import entry, PreflightRefused
        try:
            entry(run.legs.perp, run.inst, sim=run.legs.sim,
                  leverage=run.cfg.get("perp.hyperliquid.leverage"),
                  capacity=D(str(run.spec.get("hedge_capacity") or 0)), fee_rate=run.legs.fee_rate,
                  reserve=_lim(run.cfg, "min_hl_margin_reserve_usdc"),
                  expected_short=deal_book(self.con, run.did).short, book_levels=BOOK_LEVELS)
        except PreflightRefused as e:
            raise Pause(e.code, str(e)) from None

    def _requote(self, run: SolRun, amount_raw: int):
        """Свежий сбор у кнопки (§5.1 п.7): тот же запрос и политика best; победитель — в одобренных границах (R13)."""
        con, legs, cfg, inst = self.con, run.legs, run.cfg, run.inst
        side = run.kind
        req = self.desk.request(legs, inst, side, amount_raw, cfg, side)
        bk = deal_book(con, run.did)
        avail = None
        if side == "entry":
            try:
                avail = legs.perp.margin().available
            except Exception:         # noqa
                avail = None
        dec = legs.router.select(req, prices=self.desk.prices(legs), block_height=legs.block_height,
                                 hedge=self.desk.hedge_fn(legs, inst, cfg, side, available=avail,
                                                          close_qty=bk.short if side == "exit" else None),
                                 inflight_unknown=self.desk.inflight(con, legs))
        if run.op_id:
            try:
                store.append_route_rounds(con, dec.records(run.op_id, 1))
            except store.StoreError as e:
                log.warning("route_candidates %s: %s", run.op_id, e)
        w, _wr = self.desk.pick(dec, legs.sim)
        if w is None:
            raise Pause("route", f"нет проверенного маршрута ({'; '.join(dec.reasons) or '—'}) — ничего не отправлено")
        if dec.winner is None:
            dec = replace(dec, winner=w)
        ap = Approval(**{k: (D(v) if k in ("max_cost_per_token", "min_net_usdc") and v is not None else v)
                         for k, v in (run.spec.get("approval") or {}).items()})
        ok, why, audit = reselect_allowed(ap, dec)
        store.event(con, "route_reselect", deal_id=run.did, intent_id=run.iid, **audit)
        if not ok:
            self.e.hooks.requote(run.iid, ", ".join(why))
            raise Pause("requote", f"маршрут у кнопки вне одобренного ({', '.join(why)}) — ничего не отправлено, нужен "
                                   "новый план")
        if not legs.sim:
            late = legs.router.presign_check(dec, block_height=legs.block_height())
            if late:
                raise Pause("route", f"котировка устарела до подписи ({', '.join(late)}) — ничего не отправлено")
        return dec, req

    def _swap(self, run: SolRun, clip_id: int, dec, req) -> SwapOutcome:
        con, legs, w = self.con, run.legs, dec.winner
        amount = int(w.amount_in_raw)
        if not legs.sim:              # S20: запас SOL после всех обязательных расходов — до свопа
            need = native_cash_needed(w.fees, legs.wallet, _lim(run.cfg, "min_sol_reserve_lamports"))
            have = legs.spot.native_balance()
            if need is None or have is None or have < need:
                raise Pause("native", f"SOL {have if have is not None else 'не прочитан'} лампортов — меньше расходов "
                                      f"с резервом {need if need is not None else '(неизвестно)'}: своп не начинаю")
        OperationController(con).begin_spot(clip_id, operation_id=run.op_id, reserve_raw=amount)
        logical = f"{run.op_id or run.iid}:{run.iid}:{run.seq}"
        meta = dict(deal_id=run.did, op_id=run.op_id, clip_id=clip_id, intent_id=run.iid)

        def apply(o: SwapOutcome) -> None:
            apply_swap(con, run.did, clip_id, run.op_id, amount, o)
        try:
            out = legs.spot.swap(con, w, req, logical_action_id=logical, clip_ref=str(clip_id), meta=meta,
                                 min_validity_heights=_lim(run.cfg, "min_blockhash_validity_heights"), apply=apply)
        except PresendRefused as e:
            apply(SwapOutcome("not_sent", None, None, reason=str(e)))
            store.event(con, "dex_not_sent", deal_id=run.did, intent_id=run.iid, clip_id=clip_id, err=redact(e))
            raise Pause("dex_refused", f"своп не отправлен: {redact(e)}") from None
        except Exception as e:        # noqa
            if not legs.sim and legs.spot.touched(con, logical):
                store.set_clip_state(con, clip_id, ClipState.DEX_UNKNOWN)
                raise Pause("dex_unknown", f"исход свопа неизвестен: {type(e).__name__}: {redact(e)}") from None
            apply(SwapOutcome("not_sent", None, None, reason=type(e).__name__))
            raise Pause("dex_refused", f"своп не отправлен: {type(e).__name__}: {redact(e)}") from None
        run.swap = out
        store.event(con, "dex", deal_id=run.did, intent_id=run.iid, clip_id=clip_id, status=out.state, tx=out.signature,
                    a_in=out.in_raw, a_out=out.out_raw, path=out.path, reason=out.reason)
        if out.state == "ok":
            return out
        if out.state == "failed":
            raise Pause("dex_failed", f"своп упал в сети ({out.reason}) — токены не двигались, сеть оплачена")
        if out.state in ("expired", "not_sent"):
            raise Pause("dex_expired", f"своп не вошёл в блок ({out.reason}) — токены не двигались")
        c = store.get_clip(con, clip_id)
        if c is not None and c["state"] == ClipState.DEX_SENT:
            store.set_clip_state(con, clip_id, ClipState.DEX_UNKNOWN)
        raise Pause("dex_unknown", f"исход свопа неизвестен: {out.reason} — новых отправок нет")

    def _hl_hedge(self, run: SolRun, clip_id: int, side: str, qty: D, reduce_only: bool) -> HedgeResult:
        """Хедж дочерними IOC по ФАКТУ спота (hedge=True: ставится и на паузе). Кэп — от лучшей цены первой книги с
        допуском владельца, следующие дочерние не хуже него; округление цены — правилом HL в сторону допуска.
        Недобор — только разница по новой книге; UNKNOWN — settle_unknown по сохранённому cloid, повтор — только
        после доказанного «не выставлена». Попытки и срок — лимиты владельца."""
        con, perp, cfg, v = self.con, run.legs.perp, run.cfg, _views()
        store.set_clip_state(con, clip_id, ClipState.PERP_SENT)
        slip = _lim(cfg, "max_hedge_slippage_bps")
        if slip is None and not run.legs.sim:
            return HedgeResult(ZERO, ZERO, "deficit", "не задан max_hedge_slippage_bps")
        tries = _lim(cfg, "max_hedge_attempts")
        tries = int(tries) if tries is not None else PERP_ATTEMPTS_MAX
        ddl = _lim(cfg, "hedge_deadline_ms")
        end = self.e.clock() + float(ddl) / 1000 if ddl is not None else None
        short0 = deal_book(con, run.did).short or ZERO
        remaining, filled, quote = qty, ZERO, ZERO
        cap0 = None
        child = 0
        known: list[int] = []
        while remaining > 0:
            if child >= tries or (end is not None and self.e.clock() > end):
                why = "попытки кончились" if child >= tries else "срок хеджа вышел"
                return HedgeResult(filled, quote, "deficit",
                                   f"{why}: не хватает {v.tok(remaining)} (кэп {v.px(cap0)})")
            try:
                book = perp.book(run.symbol, BOOK_LEVELS)
            except Exception as e:    # noqa
                return HedgeResult(filled, quote, "deficit", f"стакан не прочитан: {redact(e)}")
            lv = book.bids if side == "SELL" else book.asks
            if not lv:
                return HedgeResult(filled, quote, "deficit", "стакан пуст — объём неизвестен")
            best = lv[0][0]
            raw_cap = best * (1 - D(slip) / BPS) if (side == "SELL" and slip is not None) else \
                best * (1 + D(slip) / BPS) if slip is not None else best
            cap = perp.quantize_px(run.symbol, raw_cap, side)
            if cap0 is None:
                cap0 = cap
            cap = max(cap, cap0) if side == "SELL" else min(cap, cap0)
            q = perp.floor_qty(run.symbol, remaining)
            if q <= 0:
                break
            if not reduce_only and q * cap < R.MIN_NOTIONAL_USD:
                return HedgeResult(filled, quote, "deficit", f"остаток {v.tok(q)} меньше минимального ордера HL "
                                                             f"{R.MIN_NOTIONAL_USD} $ — шорт ради минимума не "
                                                             "увеличиваю")
            child += 1
            cid = store.client_order_id(run.did, run.letter, clip_id, child, 1)
            since_ms = int(self.e.clock() * 1000)
            pos_before = -(short0 + (filled if side == "SELL" else -filled))
            try:
                from .adapters.execution import submit_ioc
                fill = submit_ioc(con, deal=run.deal, clip_id=clip_id, native=perp,
                                  account=run.legs.account_id, fill_venue=run.legs.fill_venue,
                                  client_id=cid, side=side, quantity=q, price=cap,
                                  reduce_only=reduce_only, clock=self.e.clock,
                                  authorize=lambda leg, action: self._agent_gate(run, "hedge"),
                                  registry=getattr(self.e.legs, 'adapters', None))
            except Exception as e:    # noqa — до отправки (ворота, параметры, незакрытая заявка) — или после подписи
                row = store.get_perp_order(con, cid)
                if row is None or row["state"] in (PerpOrderState.INTENT, PerpOrderState.NOT_PLACED):
                    if row is not None and row["state"] == PerpOrderState.INTENT:
                        store.perp_order_result(con, cid, PerpOrderState.NOT_PLACED, err=f"{type(e).__name__}: {e}"[:200])
                    return HedgeResult(filled, quote, "deficit", f"заявка не отправлена: {redact(e)}")
                fill = None
            if fill is None or fill.status == "UNKNOWN":
                store.perp_order_result(con, cid, PerpOrderState.UNKNOWN, err=getattr(fill, "err_text", None))
                store.event(con, "perp_unknown", deal_id=run.did, intent_id=run.iid, clip_id=clip_id, cid=cid)
                from .adapters.execution import settle_ioc
                s = settle_ioc(con, deal=run.deal, native=perp, account=run.legs.account_id,
                               client_id=cid, pos_before=pos_before, since_ms=since_ms,
                               known_order_ids=frozenset(known))
                if s.status == "NOT_FOUND":
                    store.perp_order_result(con, cid, PerpOrderState.NOT_PLACED, err=getattr(s, "err_text", None) or
                                            "не выставлена")
                    continue
                if s.status not in ("FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED"):
                    return HedgeResult(filled, quote, "unknown", f"исход заявки {cid} неизвестен — НЕ повторяю")
                fill = s
            store.record_perp_fill(con, fill,
                                   err=getattr(fill, "err_kind", None) if fill.status == "REJECTED" else None)
            filled += fill.qty
            quote += fill.quote
            remaining -= fill.qty
            if fill.order_id is not None:
                known.append(int(fill.order_id))
            if fill.status == "REJECTED":
                kind = getattr(fill, "err_kind", None) or "other"
                from .adapters.outcomes import rejection
                st = "reduce_only_reject" if rejection(fill) == "reduce_only" else "deficit"
                return HedgeResult(filled, quote, st, f"Hyperliquid: {kind} {getattr(fill, 'err_text', '') or ''}"
                                   .strip())
            if remaining > 0:
                self.e.sleep(float(planner.TAU_P_S))
        run.perp_filled += filled
        run.perp_quote += quote
        return HedgeResult(filled, quote, "ok")

    def _after_hedge(self, run: SolRun, clip_id: int, hr: HedgeResult) -> None:
        con = self.con
        if hr.status != "ok":
            run.perp_filled += hr.filled
            run.perp_quote += hr.quote
        fields = {"perp_qty": hr.filled, "perp_quote": hr.quote}
        if hr.status == "ok":
            store.set_clip_state(con, clip_id, ClipState.BALANCED, **fields)
            return
        if hr.status == "unknown":
            store.set_clip_state(con, clip_id, ClipState.PERP_SENT, **fields)
            raise Pause("perp_unknown", hr.text or "исход заявки неизвестен")
        store.set_clip_state(con, clip_id, ClipState.HEDGE_DEFICIT, **fields)
        raise Pause("reduce_only_reject" if hr.status == "reduce_only_reject" else "hedge_deficit",
                    f"перп не добран: {hr.text}")

    def _invariant(self, run: SolRun, position: bool = True):
        bk = deal_book(self.con, run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        d = _delta(run.inst, bk.tokens(run.dec), bk.short)
        if not (ZERO <= d < _tokens_per_step(run.inst, run.step)):
            raise Pause("hedge_deficit", f"дельта ног {_views().tok(d, True)} — вне [0, шаг)")
        if position and not run.legs.sim:
            pos = None
            for i in range(3):
                pos = run.legs.perp.position(run.symbol)
                if pos is not None:
                    break
                self.e.sleep(1.0)
            if pos is None:
                raise Pause("position_unknown", "позиция HL не прочитана — ноги не сверить")
            if pos != -bk.short:
                raise Pause("position_mismatch", f"позиция HL {pos} ≠ журнал сделки {-bk.short}")
        return bk

    # --- вход ---
    def _entry(self, run: SolRun) -> None:
        con = self.con
        self.guard(run, entry=True)
        self._deal_to(run, DealState.ENTERING)
        self._op_start(run)
        self._hl_preflight(run)
        amount = int(run.spec["amount_raw"])
        self._run_spot_clips(run, amount, self._entry_hedge)

    def _run_spot_clips(self, run: SolRun, amount: int, hedge) -> None:
        entry = run.kind == "entry"

        def progress(seq, total):
            run.seq = seq
            self.e.current = (run.iid, seq, total)

        def spot(cid, amount, ticket):
            decision, request = ticket
            self.guard(run, entry=entry)
            run.t_swap = self.e.clock()
            self._progress(run, "swap", path=decision.winner.path)
            self._swap(run, cid, decision, request)

        def settle(cid, ticket):
            if entry:
                self._basis(run, cid)
            self._invariant(run)

        OperationController(self.con).run_clips(ClipLifecycle(
            intent_id=run.iid, amounts=[amount], progress=progress,
            guard=lambda: self.guard(run, entry=entry), select_amount=lambda u, last: u,
            prepare=lambda u, last: self._requote(run, u), spot=spot,
            hedge=lambda cid, last, ticket: hedge(run, cid), settle=settle,
            next_amounts=lambda cid, done, remaining, ticket: remaining, finish=lambda: self._finish(run)))

    def _entry_hedge(self, run: SolRun, clip_id: int) -> None:
        con = self.con
        # хедж по факту прихода (чек): «стоп» теперь не мешает — нога уже исполнена (hedge=True)
        bk = deal_book(con, run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        T, S = bk.tokens(run.dec), bk.short
        target = _target(run.inst, T, run.step)
        cap = D(str(run.spec.get("hedge_capacity") or target))
        decision = Exposure(run.inst.fs, run.inst.fp, run.step).decide(
            T, S, "entry", rounding="target", capacity=cap)
        need = decision.quantity
        if need > 0:
            self._progress(run, "hedge", tokens=_h(run.swap.out_raw, run.dec), qty=need)
            self._after_hedge(run, clip_id, self._hl_hedge(run, clip_id, "SELL", need, False))
        else:
            store.set_clip_state(con, clip_id, ClipState.BALANCED, perp_qty=ZERO, perp_quote=ZERO)
        if decision.surplus:          # G09: получено больше одобренной ёмкости — продажа излишка в пилоте выключена
            raise Pause("surplus", f"пришло больше одобренного: шорт {cap} из нужных {target} — излишек без хеджа, "
                                   "продажа излишка в пилоте выключена")

    # --- выход ---
    def _exit(self, run: SolRun) -> None:
        con = self.con
        self.guard(run)
        bk0 = self._invariant(run)
        T0 = int(bk0.tokens_raw)
        if T0 <= 0:
            raise Pause("changed", "спота в сделке нет — «дохедж» откупит шорт")
        if not run.legs.sim:
            wal = run.legs.spot.token_balance(run.inst.token, run.inst.token_program)
            if wal is None or wal < T0:
                raise Pause("wallet_unknown" if wal is None else "position_mismatch",
                            f"кошелёк {wal} против журнала {T0} — выход не начинаю")
            self._agent_gate(run, "спот не продаю")    # продажа без откупа шорта оставила бы голый шорт
        self._deal_to(run, DealState.EXITING)
        self._op_start(run)
        op = store.get_operation(con, run.op_id) if run.op_id else None
        amount = min(T0, store.operation_remaining(op)) if op else T0
        self._run_spot_clips(run, amount, self._exit_hedge)

    def _exit_hedge(self, run: SolRun, clip_id: int) -> None:
        con = self.con
        bk = deal_book(con, run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        target = _target(run.inst, bk.tokens(run.dec), run.step)
        decision = Exposure(run.inst.fs, run.inst.fp, run.step).decide(
            bk.tokens(run.dec), bk.short, "exit", rounding="target")
        buy = decision.quantity
        if buy > 0:
            self._progress(run, "hedge", tokens=_h(run.swap.in_raw, run.dec), qty=buy)
        if buy > 0:                   # только на S − target по факту списания (возврат роутера — остаток захеджирован)
            self._after_hedge(run, clip_id, self._hl_hedge(run, clip_id, "BUY", buy, True))
        elif decision.deficit:        # U08: недохедж не лечится отрицательным BUY или неявным SELL
            store.set_clip_state(con, clip_id, ClipState.HEDGE_DEFICIT, perp_qty=ZERO, perp_quote=ZERO)
            raise Pause("hedge_deficit", f"после продажи шорт {bk.short} меньше нужного {target} — заявку не шлю")
        else:
            store.set_clip_state(con, clip_id, ClipState.BALANCED, perp_qty=ZERO, perp_quote=ZERO)

    # --- дохедж ---
    def _rehedge(self, run: SolRun) -> None:
        con = self.con
        d = store.get_deal(con, run.did)
        if d["state"] == DealState.OPEN:
            store.set_deal_state(con, run.did, DealState.PAUSED, reason="rehedge")
        elif d["state"] != DealState.PAUSED:
            raise Pause("state", f"сделка {run.did} в состоянии {d['state']}")
        self.e.current = (run.iid, 1, 1)
        self.guard(run)
        bk = deal_book(con, run.did)
        if not bk.known:
            raise Pause("book_unknown", f"книга сделки неизвестна: {bk.why}")
        target = _target(run.inst, bk.tokens(run.dec), run.step)
        decision = Exposure(run.inst.fs, run.inst.fp, run.step).decide(
            bk.tokens(run.dec), bk.short, "rehedge", rounding="target")
        side, qty = decision.side, decision.quantity
        if qty <= 0:
            return self._settle_fix(run, noop="ноги уже ровно — ничего не отправлено")
        if side != run.spec.get("side"):
            raise Pause("changed", "дельта ног сменила знак после плана — пришлите «дохедж» заново")
        if not run.legs.sim:
            pos = run.legs.perp.position(run.symbol)
            if pos is None or pos != -bk.short:
                raise Pause("position_mismatch", f"позиция HL {pos} ≠ журнал сделки {-bk.short}")
        qty = min(qty, D(str(run.spec["qty"])))
        clip_id = store.create_clip(con, run.iid, 1, 0)
        hr = self._hl_hedge(run, clip_id, side, qty, side == "BUY")
        self._after_hedge(run, clip_id, hr)
        self._invariant(run)
        self._settle_fix(run, qty=hr.filled, usd=hr.quote, side=side)

    def _settle_fix(self, run: SolRun, *, qty: D | None = None, usd: D | None = None, side: str | None = None,
                    noop: str | None = None) -> None:
        con, v = self.con, _views()
        bk = deal_book(con, run.did)
        last = con.execute("SELECT status FROM intents WHERE deal_id=? AND kind IN ('entry','exit') ORDER BY created "
                           "DESC LIMIT 1", (run.did,)).fetchone()
        dl = _delta(run.inst, bk.tokens(run.dec), bk.short) if bk.known else None
        hedged = dl is not None and ZERO <= dl < _tokens_per_step(run.inst, run.step)
        if bk.known and bk.short == 0 and bk.tokens_raw == 0:
            new = DealState.CLOSED
        elif hedged and last and last["status"] == IntentStatus.DONE:
            new = DealState.OPEN
        else:
            new = DealState.PAUSED
        self.e._set_deal(run.did, new, reason="сбалансировано" if new == DealState.PAUSED else None,
                         carry=dl if dl is not None else None, now=self.e.clock())
        store.set_intent_status(con, run.iid, IntentStatus.DONE)
        store.event(con, "fixed", deal_id=run.did, intent_id=run.iid, intent_kind=run.kind,
                    what=noop or f"{side} {qty} = {usd}", state=str(new))
        self.e.hooks.report(v.fix_done(v.FixDoneView(
            kind="rehedge", coin=run.deal["coin"], deal_id=run.did, state=str(new), qty=qty, usd=usd, side=side,
            noop=noop, delta=dl, step=_tokens_per_step(run.inst, run.step), sim=run.legs.sim, m=run.inst.m)))

    # --- итог ---
    def _basis(self, run: SolRun, clip_id: int) -> None:
        con = self.con
        c = store.get_clip(con, clip_id)
        try:
            din, dout, pq, pqq = int(c["dex_in"]), int(c["dex_out"]), dget(c["perp_qty"]), dget(c["perp_quote"])
            if not din or not dout or not pq:
                return
            spot = (D(din) / D(10) ** int(run.inst.quote_dec)) / (D(dout) / D(10) ** run.dec * run.inst.fs)
            perp = pqq / pq / run.inst.fp
            store.set_clip_state(con, clip_id, c["state"], basis_bps=float((perp / spot - 1) * BPS))
        except (TypeError, ValueError, ArithmeticError):
            return

    def _finish(self, run: SolRun) -> None:
        con, cfg = self.con, run.cfg
        bk = self._invariant(run)
        T, S = bk.tokens(run.dec), bk.short
        dust = False
        if run.kind == "entry":
            new = DealState.OPEN
            op_state = OpState.OPEN
        elif S == 0 and bk.tokens_raw == 0:
            new, op_state = DealState.CLOSED, OpState.CLOSED
        elif S == 0:                  # остаток меньше шага перпа: закрыть — только в обеих границах пыли владельца
            px = (D(run.swap.out_raw) / D(10) ** int(run.inst.quote_dec)) / (D(run.swap.in_raw) / D(10) ** run.dec) \
                if (run.swap and run.swap.in_raw) else None
            mt, mu = _lim(cfg, "max_dust_tokens"), _lim(cfg, "max_dust_usdc")
            dust = px is not None and mt is not None and mu is not None and T <= D(mt) and T * px <= D(mu)
            new, op_state = (DealState.CLOSED, OpState.CLOSED) if dust else (DealState.OPEN, OpState.PARTIAL)
        else:                         # роутер вернул часть: остаток сделки захеджирован, сделка открыта
            new, op_state = DealState.OPEN, OpState.PARTIAL
        fields = {"carry": _delta(run.inst, T, S)}
        if new == DealState.CLOSED:
            fields["dust"] = T
        self.e._set_deal(run.did, new, now=self.e.clock(), **fields)     # часы исполнения: граница окна учёта HL
        _op_to(con, run.op_id, op_state, reason=None if op_state != OpState.PARTIAL else "остаток после выхода")
        if run.kind == "exit" and new != DealState.CLOSED:
            store.event(con, "exit_residual", deal_id=run.did, intent_id=run.iid, tokens=T, short=S)
        store.set_intent_status(con, run.iid, IntentStatus.PARTIAL if op_state == OpState.PARTIAL else IntentStatus.DONE)
        warn = []
        if run.t_swap is not None:
            naked_ms = int((self.e.clock() - run.t_swap) * 1000)
            lim = _lim(cfg, "max_unhedged_ms")
            store.event(con, "unhedged", deal_id=run.did, intent_id=run.iid, ms=naked_ms, limit_ms=lim)
            if lim is not None and naked_ms > int(lim):
                warn.append(f"нога была без хеджа {_views().dur(naked_ms / 1000)} — дольше лимита "
                            f"{_views().dur(int(lim) / 1000)}")
        hashes = self._hl_hashes(run)           # заодно добор fills/фандинга HL в учёт (факт комиссии, H15)
        pnl = None
        if new == DealState.CLOSED:              # итог сделки по накопительной книге (G10): каждое событие — один раз
            pnl = self._deal_total(run)
        _net_sol, net_usd, perp_fee = self._costs(run)
        cost = (net_usd + perp_fee) if (net_usd is not None and perp_fee is not None) else None
        snapshot = self._final(run, str(new), bk, dust, tuple(warn), hashes=hashes, pnl=pnl)
        store.event(con, "final", deal_id=run.did, intent_id=run.iid, state=str(new), sim=run.legs.sim,
                    path=run.swap.path if run.swap else None, signature=run.swap.signature if run.swap else None,
                    cost_usd=cost, unit="USDC")
        self.e.hooks.final_report(snapshot)

    def _progress(self, run: SolRun, stage: str, *, path: str | None = None, tokens: D | None = None,
                  qty: D | None = None) -> None:
        """Прогресс клипа владельцу (одно сообщение, правится): своп отправлен → спот получен, иду на перп."""
        try:
            sv = _sv()
            self.e.hooks.progress(run.iid, sv.progress(sv.SolProgressView(
                kind=run.kind, coin=run.deal["coin"], fullcoin=run.symbol, stage=stage, path=path, tokens=tokens,
                perp_qty=qty, sim=run.legs.sim)))
        except Exception as e:        # noqa — текст не мешает исполнению
            log.warning("прогресс %s: %s", run.iid, type(e).__name__)

    def _hl_hashes(self, run: SolRun) -> tuple[str, ...]:
        """Хэши fills HL заявок этого намерения (ссылки в итоге). Сначала добор fills/фандинга счёта в учёт; сбой —
        без ссылок (учёт догрузит сверка или оценка)."""
        if run.legs.sim or not run.perp_filled:
            return ()
        from . import sol_ledger
        con = self.con
        try:
            sol_ledger.ingest(con, store.get_deal(con, run.did), run.legs.perp, now=self.e.clock())
            sc = sol_ledger.scope_of(run.deal)
            cl = [r[0] for r in con.execute("SELECT cloid FROM hl_order_attempts WHERE intent_id=? AND cloid IS NOT "
                                            "NULL", (run.iid,))]
            if sc is None or not cl:
                return ()
            q = ",".join("?" * len(cl))
            return tuple(dict.fromkeys(r[0] for r in con.execute(
                f"SELECT hash FROM hl_fills WHERE network=? AND account=? AND coin=? AND cloid IN ({q}) AND hash IS "
                f"NOT NULL ORDER BY time, tid", (*sc, *cl))))
        except Exception as e:        # noqa
            log.warning("fills HL %s: %s", run.iid, type(e).__name__)
            return ()

    def _deal_total(self, run: SolRun) -> tuple[D | None, bool]:
        """Итог закрытой сделки (sol_ledger.final_mark) — и строка итога в deal_marks для кабинета."""
        from . import marks, sol_ledger
        try:
            m = sol_ledger.final_mark(self.con, store.get_deal(self.con, run.did), run.legs, now=self.e.clock(),
                                      fetch_history=False)
            if m.pnl_now is not None:
                marks.save(self.con, m)
            return m.pnl_now, bool(m.flags.get("accounting_complete"))
        except Exception as e:        # noqa
            log.warning("итог сделки %s: %s", run.did, type(e).__name__)
            return None, False

    def _costs(self, run: SolRun) -> tuple[D | None, D | None, D | None]:
        """(сеть Solana в SOL, она же в USDC по цене SOL, комиссия HL по ставке) клипа этого намерения."""
        o = run.swap
        if o is None:
            return None, None, None
        kinds = ("network_total", "tip", "rent_nonrefundable")     # невозвратное SOL из чека (как sol_ledger)
        lam = sum((f.amount_raw or 0) for f in (o.fees or ()) if f.kind in kinds
                  and f.asset == NATIVE_SOL and not f.superseded)
        unknown = any(f.amount_raw is None for f in (o.fees or ()) if f.kind in kinds)
        net_sol = None if (not o.fees or unknown) else _h(lam, 9)
        npx = run.legs.native_px()
        rate = run.legs.fee_rate()
        net_usd = (net_sol * npx) if (net_sol is not None and npx) else None
        perp_fee = (run.perp_quote * rate) if rate is not None else None
        return net_sol, net_usd, perp_fee

    def _final(self, run: SolRun, state: str, bk, dust: bool, warn: tuple = (), *, hashes: tuple = (),
               pnl: tuple | None = None):
        from ..ipc.reports import SolFinalView
        o, inst = run.swap, run.inst
        qd = int(inst.quote_dec)
        if run.kind == "entry":
            tokens, usdc = _h(o.out_raw, run.dec), _h(o.in_raw, qd)
        else:
            tokens, usdc = _h(o.in_raw, run.dec), _h(o.out_raw, qd)
        net_sol, net_usd, perp_fee = self._costs(run)
        pxp = (run.perp_quote / run.perp_filled) if run.perp_filled else None
        basis = None
        if run.kind == "entry" and pxp and tokens and usdc:
            basis = ((pxp / inst.fp) / (usdc / (tokens * inst.fs)) - 1) * BPS
        rest = bk.tokens(run.dec) if run.kind == "exit" else None
        spx = (usdc / tokens) if (usdc and tokens) else None
        dl = _delta(inst, bk.tokens(run.dec), bk.short)
        return SolFinalView(
            kind=run.kind, coin=run.deal["coin"], fullcoin=inst.perp_symbol, deal_id=run.did, state=state,
            tokens=tokens, usdc=usdc, path=o.path, perp_qty=run.perp_filled, perp_px=pxp, basis_bps=basis,
            net_sol=net_sol, net_usd=net_usd, perp_fee_usd=perp_fee, rest_tokens=rest,
            rest_usd=(rest * spx) if (rest and spx) else None, dust=dust,
            hedged=ZERO <= dl < _tokens_per_step(inst, run.step), sim=run.legs.sim, warn=warn,
            signature=o.signature, hl_hashes=tuple(hashes), pnl_usdc=pnl[0] if pnl else None,
            pnl_complete=bool(pnl[1]) if pnl else True)

    # --- пауза ---
    def _paused(self, run: SolRun, p: Pause) -> None:
        con = self.con
        deal = store.get_deal(con, run.did)
        bk = deal_book(con, run.did)
        progressed = self.e._progressed(run.iid)
        empty = bk.tokens_raw == 0 and bk.short == 0
        if deal["state"] == DealState.DRAFT:
            target = DealState.ABORTED
        elif p.reason in ("position_mismatch", "book_unknown"):
            target = DealState.HALTED_MISMATCH
        elif run.kind == "entry" and empty and deal["state"] == DealState.ENTERING and not progressed:
            target = DealState.ABORTED
        else:
            target = DealState.PAUSED
        self.e._set_deal(run.did, target, reason=p.reason, now=self.e.clock())
        store.set_intent_status(con, run.iid, IntentStatus.PARTIAL if progressed else IntentStatus.FAILED, err=p.text)
        op = store.get_operation(con, run.op_id) if run.op_id else None
        if op is not None:
            if op["state"] == OpState.PROPOSED:
                _op_to(con, run.op_id, OpState.APPROVED)
            unknown = p.reason in _UNKNOWN_REASONS or op["reserved_raw"] != "0"
            if unknown:
                _op_to(con, run.op_id, OpState.PAUSED_UNKNOWN, reason=p.reason)
            elif not progressed and not int(op["confirmed_raw"]):
                _op_to(con, run.op_id, OpState.STOPPED, OpState.ABANDONED, reason=p.reason)
            elif p.reason in ("stop", "terminate"):
                _op_to(con, run.op_id, OpState.STOPPED, reason=p.reason)
            else:
                _op_to(con, run.op_id, OpState.PAUSED_RISK, reason=p.reason)
        store.event(con, "paused", deal_id=run.did, intent_id=run.iid, reason=p.reason, text=p.text, state=str(target))
        dl = _delta(run.inst, bk.tokens(run.dec), bk.short) if bk.known else None
        if dl is not None and ZERO <= dl < _tokens_per_step(run.inst, run.step):
            dl = ZERO
        need = None
        if bk.known:
            need = _target(run.inst, bk.tokens(run.dec), run.step) - bk.short
        px = None
        try:
            px = planner.mid(run.legs.perp.book(run.symbol, 5))
        except Exception:             # noqa
            pass
        pos = wal = None
        try:
            if run.legs.sim:
                pos = None if bk.short is None else -bk.short
            else:
                pos = run.legs.perp.position(run.symbol)
        except Exception:             # noqa
            pos = None
        if run.legs.sim:
            wal = bk.tokens(run.dec)
        else:
            u = run.legs.spot.token_balance(run.inst.token, run.inst.token_program)
            wal = _h(u, run.dec)
        sv = _sv()
        self.e.hooks.report(sv.halt(sv.SolHaltView(
            kind=run.kind, coin=run.deal["coin"], deal_id=run.did, intent_id=run.iid, reason=p.text, state=str(target),
            wallet_tokens=wal, perp_pos=pos, delta=dl,
            delta_usd=(abs(dl) * px * run.inst.fs / run.inst.fp) if (dl and px) else None, need_qty=need,
            sim=run.legs.sim)))


def _plan(it: dict) -> Plan:
    from .engine import plan_from_json
    return plan_from_json(it["plan_json"])
