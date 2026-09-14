"""Сверка сделок с правдой — цепью и биржей (trade_spec §6 «Restart» и «позиции»). Только чтение сети: здесь нет
ни одной отправки — исход неизвестного выясняется запросом, а не повтором (урок SentUnknown из lphedge).

Порядок (почему именно так):
  1. dex_txs с неизвестным исходом — по nonce-группе: чек по хэшу; на nonce замайнилась наша замена (bump/cancel) —
     остальные REPLACED; nonce свободен и хэша никто не знает — DROPPED; nonce занят НЕ нашей транзакцией — стоп
     (ключ кошелька у кого-то ещё); ещё в пуле или сеть молчит — исход неизвестен.
  2. клипы DEX_SENT/DEX_UNKNOWN — по своим транзакциям: замайнен своп → DEX_OK с суммами из логов Transfer;
     не подписан / выброшен / откатился / отменён → DEX_REVERTED (токены не двигались); иначе — неизвестно.
  3. заявки перпа: INTENT → NOT_PLACED (nonce подписи не записан — значит, запрос не уходил: on_signed стоит ДО
     отправки); SENT/UNKNOWN → settle_unknown (три -2013 за ~5 с + неизменные позиция и сделки — «не выставлена»,
     иначе найденный итог или UNKNOWN). Одна проверка -2013 доказательством не считается.
  4. добор userTrades; 5. книга сделки против balanceOf и positionRisk.

Сверка: в кошельке меньше токенов, чем по журналу, или позиция биржи ≠ −шорт журнала — расхождение
(HALTED_MISMATCH). Лишние токены в кошельке — не расхождение сделки (свои токены владельца), только заметка.
Не прочитано — None: «совпадает» при непрочитанной стороне было бы ложью.

Симуляция (sim=1): транзакций в цепи и заявок на бирже нет, виртуальное состояние SimSpot/SimPerp умерло с
процессом. Правда одна — журнал: незавершённое помечается не случившимся, ноги симуляции засеваются книгой (seed).
"""
from __future__ import annotations
import json, logging, time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable
from .ledger_flows import perp_quote_flows
from .operations import OperationController, SpotSettlement
from .. import config
from . import marks, report, store, tconfig
from .engine import DealBook, Legs, backfill_instruments, deal_book, dget
from .keys import redact
from .runtime import ProfileDown, is_sol_deal, legs_of
from .store import ClipState, DealState, DexTxState, PerpOrderState

from .adapters.evm_recovery import _resolve_nonce, _refetch_amounts

log = logging.getLogger(__name__)
D = Decimal
ZERO = D(0)

UNRESOLVED_TX = (str(DexTxState.SIGNED), str(DexTxState.SENT), str(DexTxState.UNKNOWN))
MINED = (str(DexTxState.MINED_OK), str(DexTxState.MINED_REVERTED))
FINAL_PERP = ("FILLED", "PARTIALLY_FILLED", "EXPIRED", "REJECTED")
OPEN_PERP = (str(PerpOrderState.INTENT), str(PerpOrderState.SENT), str(PerpOrderState.UNKNOWN))
FOREIGN_NONCE = "nonce занят не нашей транзакцией"


# --- итог сверки одной сделки ----------------------------------------------------------------------------
@dataclass
class DealCheck:
    """matched: True — книга сходится с цепью и биржей; False — расхождение; None — не прочитано / исход неизвестен.
    hedged: дельта ног в [0, шаг) (None — шаг или книга неизвестны)."""
    deal_id: str
    matched: bool | None
    detail: str
    book: DealBook
    wallet_units: int | None = None
    position: D | None = None
    hedged: bool | None = None
    delta: D | None = None
    notes: list[str] = field(default_factory=list)
    step: D | None = None               # шаг перпа — для «+4 902» в текстах перезапуска (заполняет startup)
    delta_usd: D | None = None          # голая нога ≈ $ по средней цене входа из журнала (startup, без сети)

    @property
    def m(self) -> D:
        """Токенов в контракте — из книги сделки (её инструмент): дельта в токенах, шаг и шорт — в контрактах.
        m не известен (ревью 13.09, M3) — 0: тексты пишут «N контр.» без пересчёта в токены."""
        return getattr(self.book, "m_view", getattr(self.book, "m", D(1)))


def _v():
    from ..tg import views            # общие форматтеры сообщений; импорт здесь — trade не тянет tg при импорте модуля
    return views


def _q(x, sign: bool = False, step=None) -> str:
    """Токены в тексте сверки — тем же форматтером, что в сообщениях: «4 902», без 18 знаков после точки."""
    return _v().tok(x, sign, step)


def _tokens_by_clip(con, deal: dict) -> dict[int, tuple[str, str]]:
    """clip_id → (вход, выход) свопа: вход — стейбл → токен, выход и откат — токен → стейбл. Нужно resolve(): по
    логам Transfer считаются суммы именно этих двух токенов."""
    stable = config.OKX_DEX_STABLES[tconfig.chain_index(deal["chain"])][0].lower()
    token = str(deal["token"]).lower()
    out = {}
    for r in con.execute("SELECT c.id, i.kind FROM clips c JOIN intents i ON c.intent_id = i.id WHERE i.deal_id=?",
                         (deal["id"],)):
        out[int(r["id"])] = (stable, token) if r["kind"] == "entry" else (token, stable)
    return out


# --- 1. транзакции DEX ------------------------------------------------------------------------------------


def resolve_txs(con, deal: dict, spot) -> list[str]:
    """Неразрешённые транзакции клипов сделки (и соседи по их nonce)."""
    tok = _tokens_by_clip(con, deal)
    if not tok:
        return []
    qs = ",".join("?" * len(tok))
    groups = con.execute(f"SELECT DISTINCT chain, wallet, nonce FROM dex_txs WHERE clip_id IN ({qs}) AND state IN "
                         f"(?,?,?) ORDER BY nonce", (*tok, *UNRESOLVED_TX)).fetchall()
    out = []
    for g in groups:
        p = _resolve_nonce(con, spot, g["chain"], g["wallet"], int(g["nonce"]), tok)
        if p:
            out.append(p)
    return out


def resolve_wallet_txs(con, spot) -> list[str]:
    """Транзакции кошелька без клипа (approve): на книгу сделки не влияют, но висящий nonce блокирует кошелёк."""
    if spot is None:
        return []
    wallet = str(getattr(spot, "wallet", "") or "").lower()
    chain = str(getattr(spot, "chain", "") or "")   # адрес общий у BSC и Robinhood: чек ищем только в сети этой ноги
    groups = con.execute("SELECT DISTINCT chain, wallet, nonce FROM dex_txs WHERE clip_id IS NULL AND wallet=? AND "
                         "chain=? AND state IN (?,?,?) ORDER BY nonce", (wallet, chain, *UNRESOLVED_TX)).fetchall()
    return [p for p in (_resolve_nonce(con, spot, g["chain"], g["wallet"], int(g["nonce"]), {}) for g in groups) if p]


# --- 2. клипы ------------------------------------------------------------------------------------------
def resolve_clips(con, deal: dict, legs: Legs) -> list[str]:
    out = []
    tok = _tokens_by_clip(con, deal)
    rows = con.execute("SELECT c.* FROM clips c JOIN intents i ON c.intent_id = i.id WHERE i.deal_id=? AND c.state IN "
                       "(?,?) ORDER BY c.id", (deal["id"], str(ClipState.DEX_SENT), str(ClipState.DEX_UNKNOWN))).fetchall()
    for c in rows:
        cid, st = int(c["id"]), c["state"]
        op = store.operation_of_intent(con, c['intent_id'])
        root = dict(operation_id=op['id'], reserve_raw=int(c['planned_in'])) if op else {}
        if legs.sim:
            OperationController(con).settle_spot(cid, SpotSettlement(False), **root)
            store.event(con, "reconcile_clip", deal_id=deal["id"], clip_id=cid, was=st,
                        why="симуляция: состояние потеряно при перезапуске — клип не случился")
            continue
        txs = [dict(r) for r in con.execute("SELECT * FROM dex_txs WHERE clip_id=? AND kind IN ('swap','bump','cancel') "
                                            "ORDER BY id", (cid,))]
        ok = next((t for t in txs if t["state"] == DexTxState.MINED_OK and t["kind"] in ("swap", "bump")), None)
        if ok is not None and (ok["amount_in"] is None or ok["amount_out"] is None):
            ok = _refetch_amounts(con, legs.spot, ok, tok.get(cid, (None, None)))
        if ok is not None and ok["amount_in"] is not None and ok["amount_out"] is not None:
            from .adapters.evm_recovery import stored_settlement
            try:
                outcome = stored_settlement(con, deal, legs.spot, ok,
                                            store.get_intent(con, c['intent_id'])['kind'])
            except Exception as error:
                out.append(f"клип {cid}: чек не подтверждён ({redact(error)[:120]})")
                continue
            OperationController(con).settle_spot(
                cid, outcome, **root)
            store.event(con, "reconcile_clip", deal_id=deal["id"], clip_id=cid, was=st, now="DEX_OK", tx=ok["tx_hash"])
            continue
        unknown = ok is not None or any(t["state"] in UNRESOLVED_TX for t in txs) \
            or any(t["state"] == DexTxState.REPLACED and FOREIGN_NONCE in (t["err"] or "") for t in txs)
        if unknown:
            if st == ClipState.DEX_SENT:
                store.set_clip_state(con, cid, ClipState.DEX_UNKNOWN)
            out.append(f"клип {cid}: исход свопа неизвестен")
            continue
        # не подписан (строки нет — отправки не было: запись идёт ДО неё) / выброшен / откатился / отменён
        OperationController(con).settle_spot(cid, SpotSettlement(False), **root)
        store.event(con, "reconcile_clip", deal_id=deal["id"], clip_id=cid, was=st, now="DEX_REVERTED",
                    txs=[(t["tx_hash"], t["state"]) for t in txs])
    return out




# --- 3. заявки перпа --------------------------------------------------------------------------------------
def resolve_orders(con, deal: dict, legs: Legs) -> list[str]:
    prefix = f"fb-{deal['id']}-"
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM perp_orders WHERE substr(client_id, 1, ?)=? ORDER BY id", (len(prefix), prefix))]
    open_ = [r for r in rows if r["state"] in OPEN_PERP]
    if not open_:
        return []
    out = []
    if legs.sim:
        for r in open_:
            if r["state"] == PerpOrderState.SENT:
                store.perp_order_result(con, r["client_id"], PerpOrderState.UNKNOWN)
            store.perp_order_result(con, r["client_id"], PerpOrderState.NOT_PLACED,
                                    err="симуляция: состояние потеряно при перезапуске")
        return out
    perp = legs.perp
    short_known, known_ids = ZERO, set()
    for r in rows:
        if r["state"] not in OPEN_PERP:
            q = dget(r["executed_qty"]) or ZERO
            short_known += q if r["side"] == "SELL" else -q
            if r["order_id"] is not None:
                known_ids.add(int(r["order_id"]))
    sent = [r for r in open_ if r["state"] != PerpOrderState.INTENT]
    for r in open_:
        cid = r["client_id"]
        if r["state"] == PerpOrderState.INTENT:
            # nonce подписи не записан → on_signed не выполнился → запрос не уходил
            store.perp_order_result(con, cid, PerpOrderState.NOT_PLACED, err="не отправлена (перезапуск до подписи)")
            continue
        pos_before = -short_known if len(sent) == 1 else None
        since_ms = int(float(r["sent_ts"] or time.time()) * 1000)
        try:
            from .adapters.execution import settle_ioc, recover_not_submitted
            from .adapters.execution_scope import account_for
            account = account_for(con, deal, perp)
            if recover_not_submitted(con, deal=deal, clip_id=r['clip_id'], native=perp,
                                     account=account, client_id=cid):
                continue
            f = settle_ioc(con, deal=deal, native=perp, account=account, client_id=cid,
                           pos_before=pos_before, since_ms=since_ms, known_order_ids=frozenset(known_ids))
        except Exception as e:                 # noqa
            out.append(f"заявка {cid}: не запрошена ({redact(e)[:120]})")
            continue
        if f.status in FINAL_PERP:
            store.record_perp_fill(con, f, err=getattr(perp, "last_error", None) if f.status == "REJECTED" else None)
            if f.order_id is not None:
                known_ids.add(int(f.order_id))
            store.event(con, "reconcile_order", deal_id=deal["id"], cid=cid, status=f.status, qty=f.qty)
            continue
        if r["state"] == PerpOrderState.SENT:
            store.perp_order_result(con, cid, PerpOrderState.UNKNOWN, err=getattr(perp, "last_error", None))
        if f.status == "NOT_FOUND":
            store.perp_order_result(con, cid, PerpOrderState.NOT_PLACED,
                                    err=("-2013 трижды, позиция и сделки неизменны — не выставлена (сверка)"
                                         if getattr(perp, "venue", "aster") == "aster" else
                                         "нет ни в заявках, ни в сделках, позиция неизменна — не выставлена (сверка)"))
            store.event(con, "reconcile_order", deal_id=deal["id"], cid=cid, status="NOT_PLACED")
            continue
        out.append(f"заявка {cid}: исход неизвестен")
    return out


def backfill(con, deal: dict, legs: Legs) -> str | None:
    if legs.sim:
        return None
    venue = legs.fill_venue
    try:
        from .accounting import sync_fills
        sync_fills(con, deal, legs)
    except Exception as e:                     # noqa — отчёт возьмёт итоги заявок; на сверку не влияет
        return f"userTrades не добраны: {redact(e)[:120]}"
    return None


# --- 4–5. сверка сделки ---------------------------------------------------------------------------------
def _step(legs: Legs, symbol: str) -> D | None:
    try:
        return legs.perp.filters(symbol).step
    except Exception:                          # noqa
        return None


def check_deal(con, deal: dict, legs: Legs | None, *, resolve: bool = True, seed: bool = False) -> DealCheck:
    """Сверка одной сделки. resolve — выяснять неизвестные исходы (только когда исполнитель эту сделку не ведёт);
    seed — засеять ноги симуляции книгой (только на старте: посреди исполнения это испортило бы симуляцию)."""
    did = deal["id"]
    if legs is None:
        return DealCheck(did, None, "живая сделка, а ключей для чтения нет (режим dry) — не сверена",
                         deal_book(con, did))
    problems: list[str] = []
    notes: list[str] = []
    if resolve:
        if not legs.sim:
            problems += resolve_txs(con, deal, legs.spot)
        problems += resolve_clips(con, deal, legs)
        problems += resolve_orders(con, deal, legs)
        b = backfill(con, deal, legs)
        if b:
            notes.append(b)
    bk = deal_book(con, did)
    dec = int(deal["token_dec"])
    step = _step(legs, deal["symbol"])
    ts = None if step is None else bk.tstep(step)      # шаг перпа в токенах: токены и дельта — в токенах (ревью 13.09)
    delta = bk.delta(dec)
    hedged = None if step is None else bk.hedged(dec, step)
    coin = deal["coin"]
    ctr = lambda q, sign=False: _v().contracts(q, bk.m_view, sign, step)    # noqa: E731 — шорт и позиция: контракты
    if legs.sim:
        if seed and bk.known:
            for obj, fn, args in ((legs.spot, "seed", (deal["token"], int(bk.tokens_raw))),
                                  (legs.perp, "seed", (deal["symbol"], -bk.short))):
                f = getattr(obj, fn, None)
                if f is not None:
                    f(*args)
        matched = True if (bk.known and not problems) else None
        detail = "; ".join(problems) if problems else (
            f"симуляция: по журналу {_q(bk.tokens(dec), step=ts)} {coin}, шорт {ctr(bk.short)}" if bk.known
            else (bk.why or ""))
        chk = DealCheck(did, matched, detail, bk, hedged=hedged, delta=delta, notes=notes)
        return _hedge_note(chk, coin, step)
    wal = pos = None
    try:
        v = legs.spot.balances(deal["token"]).get("token")
        wal = None if v is None else int(v)
    except Exception as e:                     # noqa
        notes.append(f"кошелёк: {redact(e)[:100]}")
    try:
        pos = legs.perp.position(deal["symbol"])
    except Exception as e:                     # noqa
        notes.append(f"позиция: {redact(e)[:100]}")
    if problems or not bk.known:
        return DealCheck(did, None, "; ".join(problems) or (bk.why or "книга неизвестна"), bk, wal, pos, hedged, delta,
                         notes)
    if wal is None or pos is None:
        what = " и ".join(x for x, y in (("кошелёк", wal), ("позиция биржи", pos)) if y is None)
        return DealCheck(did, None, f"не прочитано: {what}", bk, wal, pos, hedged, delta, notes)
    mism = []
    if wal < bk.tokens_raw:
        mism.append(f"в кошельке {_q(D(wal) / D(10) ** dec, step=ts)} < по журналу "
                    f"{_q(bk.tokens(dec), step=ts)} {coin}")
    if pos != -bk.short:                       # контракты биржи = контракты журнала
        mism.append(f"позиция {deal['perp_venue']} {ctr(pos, True)} ≠ журнал {ctr(-bk.short, True)}")
    if mism:
        return DealCheck(did, False, "; ".join(mism), bk, wal, pos, hedged, delta, notes)
    extra = wal - bk.tokens_raw
    detail = f"кошелёк {_q(bk.tokens(dec), step=ts)} {coin}, шорт {ctr(bk.short)} — как в журнале"
    if extra > 0:
        detail += f" (в кошельке ещё {_q(D(extra) / D(10) ** dec, step=ts)} {coin} не из сделки)"
    return _hedge_note(DealCheck(did, True, detail, bk, wal, pos, hedged, delta, notes), coin, step)


def _hedge_note(chk: DealCheck, coin: str, step: D | None = None) -> DealCheck:
    if not getattr(chk.book, "m_known", True):  # ревью 13.09, M3: без советов «дохедж»/«откат» — дельта не известна
        chk.detail += f"; {_v().m_unknown_text(chk.deal_id)}"
        return chk
    if chk.hedged is False and chk.delta is not None:
        side = "голый лонг" if chk.delta > 0 else "голый шорт"
        ts = None if step is None else step * chk.m                     # дельта — в токенах
        chk.detail += f"; ноги не ровно: {side} {_q(chk.delta, True, ts)} {coin} — «дохедж» или «откат»"
    return chk


# --- старт ------------------------------------------------------------------------------------------------
@dataclass
class DealRestart:
    deal: dict
    check: DealCheck
    old: str
    new: str
    intents: list[dict]                 # прерванные этим перезапуском намерения сделки


@dataclass
class StartupReport:
    interrupted: list[str]
    expired: list[str]
    aborted_drafts: list[str]
    deals: list[DealRestart]
    wallet_problems: list[str]
    inst_backfill: list = field(default_factory=list)    # (сделка, InstrumentSpec) — записан вердикт миграции


def _decide(old: str, chk: DealCheck, interrupted: bool, dec: int, step: D | None) -> tuple[str, str | None, dict]:
    strict = interrupted or old in (DealState.ENTERING, DealState.EXITING)
    bk = chk.book
    if chk.matched is True:
        if old == DealState.ENTERING and bk.tokens_raw == 0 and bk.short == 0:
            return DealState.ABORTED, "перезапуск: ничего не куплено — сделка снята", {}
        if old == DealState.EXITING and bk.short == 0 and step is not None and bk.tokens(dec) < bk.tstep(step):
            return DealState.CLOSED, None, {"dust": bk.tokens(dec), "carry": bk.delta(dec)}
        if old in (DealState.ENTERING, DealState.EXITING):
            return DealState.PAUSED, "restart", {}
        return old, None, {}
    if chk.matched is False or strict:
        return DealState.HALTED_MISMATCH, f"restart: {chk.detail}"[:300], {}
    return old, None, {}


def _entry_px(con, deal: dict) -> D | None:
    """Средняя цена входа ($ за токен) по чекам клипов входа — оценка голой ноги в $ на старте без чтения сети.
    Чеков нет или что-то не прочиталось — None («≈ $» тогда не пишется, но и не выдумывается)."""
    try:
        sdec = int(config.OKX_DEX_STABLES[tconfig.chain_index(deal["chain"])][1])
        dec = int(deal["token_dec"])
        usd = tok = ZERO
        for r in con.execute("SELECT c.dex_in, c.dex_out FROM clips c JOIN intents i ON c.intent_id=i.id "
                             "WHERE i.deal_id=? AND i.kind='entry' AND c.state IN ('DEX_OK','PERP_SENT','BALANCED',"
                             "'HEDGE_DEFICIT') AND c.dex_in IS NOT NULL AND c.dex_out IS NOT NULL", (deal["id"],)):
            usd += D(int(r[0])) / D(10) ** sdec
            tok += D(int(r[1])) / D(10) ** dec
        return usd / tok if tok > 0 else None
    except Exception as e:                     # noqa — текст перезапуска не ломает сверку
        log.warning("цена входа %s: %s", deal.get("id") if hasattr(deal, "get") else "?", redact(e))
        return None


def startup(con, legs_fn: Callable[[bool], Legs | None], *, now: float | None = None) -> StartupReport:
    """Шаги рестарта. Сам ничего не продолжает: прерванное становится interrupted, дальше — только команда
    владельца (свежий план и кнопки)."""
    ts = time.time() if now is None else now
    interrupted = store.interrupt_unfinished(con)
    expired = store.expire_intents(con, now=ts + 10 ** 9)      # кнопки прежнего процесса: котировки уже не те
    drafts = [r[0] for r in con.execute("SELECT id FROM deals WHERE state=?", (str(DealState.DRAFT),))]
    for did in drafts:
        from .operation_roots import abandon_unstarted
        with store.tx(con):
            abandon_unstarted(con, did)
            store.set_deal_state(con, did, DealState.ABORTED, expect=DealState.DRAFT, reason="не начата (перезапуск)")
    # сделки до фазы 1 (ревью 13.09): подтверждённый по журналу инструмент (m = 1) записывается один раз; боту текст
    # не нужен (C: штатное не пишем) — событие inst_backfill в журнале
    bf = backfill_instruments(con, now=ts)
    live = legs_fn(False)
    wallet_problems = resolve_wallet_txs(con, live.spot) if live is not None else []
    wallet_problems += _other_evm_wallet_txs(con, legs_fn)
    ints: dict[str, list[dict]] = {}
    for iid in interrupted:
        it = store.get_intent(con, iid)
        if it is not None:
            ints.setdefault(it["deal_id"], []).append(it)
    out = []
    for d in store.active_deals(con):
        if is_sol_deal(d):                     # связка SOL × HL: свои ноги и сверка (X12); BSC-путь ниже прежний
            out.append(_sol_restart(con, d, legs_fn, ints, ts))
            continue
        legs = _evm_legs(legs_fn, d)
        try:
            chk = check_deal(con, d, legs, resolve=True, seed=True)
        except Exception as e:                 # noqa — сверка не удалась — это «не прочитано», а не «совпало»
            log.exception("сверка %s", d["id"])
            chk = DealCheck(d["id"], None, f"сверка упала: {type(e).__name__}: {redact(e)[:120]}", deal_book(con, d["id"]))
        step = _step(legs, d["symbol"]) if legs is not None else None
        chk.step = step
        if chk.hedged is False and chk.delta is not None:      # голая нога в $ для «♻️ …» — по журналу, без сети
            px = _entry_px(con, d)
            chk.delta_usd = chk.delta * px if px is not None else None
        new, reason, fields = _decide(d["state"], chk, bool(ints.get(d["id"])), int(d["token_dec"]), step)
        if new != d["state"]:
            store.set_deal_state(con, d["id"], new, reason=reason, **fields)
        store.event(con, "restart_check", deal_id=d["id"], old=d["state"], new=str(new), matched=chk.matched,
                    detail=chk.detail, now=ts)
        try:
            from .operation_roots import adopt_legacy
            oid = adopt_legacy(con, store.get_deal(con, d['id']))
            if oid:
                op = store.get_operation(con, oid)
                if op['state'] in (store.OpState.APPROVED, store.OpState.RUNNING, store.OpState.PAUSED_UNKNOWN):
                    target = store.OpState.PAUSED_UNKNOWN if int(op['reserved_raw']) else store.OpState.STOPPED
                    store.set_operation_state(con, oid, target, reason='restart: fresh owner approval required')
        except (store.StoreError, ValueError) as e:
            store.event(con, 'operation_migration_blocked', deal_id=d['id'], why=str(e))
            if store.get_deal(con, d['id'])['state'] not in (DealState.CLOSED, DealState.ABORTED):
                store.set_deal_state(con, d['id'], DealState.HALTED_MISMATCH, reason='operation evidence incomplete')
                new = DealState.HALTED_MISMATCH
        out.append(DealRestart(deal=d, check=chk, old=d["state"], new=str(new), intents=ints.get(d["id"], [])))
    return StartupReport(interrupted, expired, drafts, out, wallet_problems, bf)


def _evm_legs(legs_fn, d: dict):
    """Ноги EVM-сделки: BSC × Aster — ровно legs_fn(sim), как раньше; иная EVM-связка (Robinhood × Gate) — ноги своей
    связки через реестр, не собраны — None («не сверена»), но никогда не ноги BSC."""
    from .owner import LEGACY_PROFILE
    from .runtime import profile_of_deal
    if profile_of_deal(d) == LEGACY_PROFILE:
        return legs_fn(bool(d["sim"]))
    return _sol_legs(legs_fn, d)[0]


def _other_evm_wallet_txs(con, legs_fn) -> list[str]:
    """Висящие транзакции кошелька (approve) EVM-связок, кроме старой: каждая — боевыми ногами своей сети."""
    from .owner import EVM_PROFILES, LEGACY_PROFILE
    out: list[str] = []
    for prof in EVM_PROFILES:
        if prof == LEGACY_PROFILE or prof not in (getattr(legs_fn, "factories", None) or {}):
            continue
        try:
            lv = legs_fn.for_profile(prof, False)
        except ProfileDown as e:
            log.warning("висящие транзакции связки %s не сверены: %s", prof, redact(e))
            continue
        if lv is not None:
            out += resolve_wallet_txs(con, lv.spot)
    return out


def _sol_legs(legs_fn, d: dict) -> tuple[Any, str | None]:
    """Ноги сделки связки SOL × HL через реестр и почему их нет: не собраны — (None, причина сбоя сборки), связки
    в процессе нет — (None, None). «Не сверена», а не ноги BSC и не «флэт»."""
    try:
        return legs_of(legs_fn, d), None
    except ProfileDown as e:                   # сбой сборки реестр помнит (last_error); «не подключена» — нет
        log.warning("ноги связки для %s: %s", d["id"], redact(e))
        built = (getattr(legs_fn, "last_error", None) or {}).get(e.profile)
        return None, (str(e.reason)[:200] if built else None)
    except Exception as e:                     # noqa — прочее: сделка остаётся видна
        log.warning("ноги связки для %s: %s", d["id"], redact(e))
        return None, f"{type(e).__name__}: {redact(e)}"[:200]


def _sol_restart(con, d: dict, legs_fn, ints: dict, ts: float) -> "DealRestart":
    from . import sol_flow
    legs, down = _sol_legs(legs_fn, d)
    try:
        chk = sol_flow.check_deal(con, d, legs, resolve=True, down=down)
    except Exception as e:                     # noqa — сверка не удалась — «не прочитано», а не «совпало»
        log.exception("сверка %s", d["id"])
        chk = DealCheck(d["id"], None, f"сверка упала: {type(e).__name__}: {redact(e)[:120]}", deal_book(con, d["id"]))
    cur = store.get_deal(con, d["id"])["state"]
    if cur == DealState.ABORTED:               # сверка сняла пустую сделку (исход выяснен): решать нечего
        new, reason, fields = DealState.ABORTED, None, {}
    else:
        new, reason, fields = _decide(d["state"], chk, bool(ints.get(d["id"])), int(d["token_dec"]), chk.step)
    if new != cur:
        store.set_deal_state(con, d["id"], new, reason=reason, **fields)
    store.event(con, "restart_check", deal_id=d["id"], old=d["state"], new=str(new), matched=chk.matched,
                detail=chk.detail, now=ts)
    return DealRestart(deal=d, check=chk, old=d["state"], new=str(new), intents=ints.get(d["id"], []))


def restart_clip(con, intent: dict) -> tuple[int | None, int | None]:
    """(последний клип, клипов по плану) — для строки «♻️ … (клип 2/3)»."""
    r = con.execute("SELECT MAX(seq) FROM clips WHERE intent_id=?", (intent["id"],)).fetchone()
    clip = int(r[0]) if r and r[0] is not None else None
    try:
        n = len(json.loads(intent["plan_json"]).get("clips") or ()) or None
    except (TypeError, ValueError):
        n = None
    return clip, (max(n, clip) if (n and clip) else n)


# --- «позиции» -------------------------------------------------------------------------------------------
def positions(con, legs_fn: Callable[[bool], Legs | None], *, now: float, busy_deal: str | None = None,
              resolve: bool = True, profile: str | None = None) -> tuple[list[dict], bool | None, str | None]:
    """Строки для views.PositionView по правде (balanceOf, positionRisk), сверка с журналом. Сделку, которую сейчас
    ведёт исполнитель, не сверяем: посреди клипа кошелёк и журнал законно расходятся. profile — только сделки одной
    связки («позиции sol»)."""
    from .runtime import profile_of_deal
    rows, verdicts, problems = [], [], []
    for d in store.active_deals(con):
        if profile is not None and profile_of_deal(d) != profile:
            continue
        dec = int(d["token_dec"])
        sol = is_sol_deal(d)                   # связка SOL × HL: свои ноги, сверка, учёт (sol_ledger)
        down = None
        if sol:
            legs, down = _sol_legs(legs_fn, d)
        else:
            legs = _evm_legs(legs_fn, d)
        if d["id"] == busy_deal:
            chk = DealCheck(d["id"], None, "идёт исполнение — сверю после", deal_book(con, d["id"]))
        else:
            try:
                if sol:
                    from . import sol_flow
                    chk = sol_flow.check_deal(con, d, legs, resolve=resolve, down=down)
                else:
                    chk = check_deal(con, d, legs, resolve=resolve)
            except Exception as e:             # noqa
                chk = DealCheck(d["id"], None, f"сверка упала: {redact(e)[:120]}", deal_book(con, d["id"]))
        bk = chk.book
        if sol and store.get_deal(con, d["id"])["state"] == DealState.ABORTED:
            continue                           # сверка сняла пустую сделку: позиции нет, строке не место
        if sol:
            rows.append(_sol_row(con, d, legs, chk, now=now,
                                 fresh=resolve and d["id"] != busy_deal and legs is not None))
            verdicts.append(chk.matched)
            if chk.matched is not True:
                problems.append(f"{d['id']}: {chk.detail}")
            continue
        spot_px = mark = rate = liq = None
        income: list[dict] = []
        if legs is not None:
            try:
                spot_px = legs.spot.pool_price(d["token"])
            except Exception:                  # noqa
                pass
            try:
                fr = legs.perp.funding(d["symbol"])
                mark, rate = fr[0], fr[1]
            except Exception:                  # noqa
                pass
            if not legs.sim and not sol:
                start = int(float(d["created"]) * 1000)
                try:
                    from .accounting import sync_funding
                    sync_funding(con, d, legs.perp)
                except Exception as e:         # noqa
                    log.warning("фандинг %s не добран: %s", d["symbol"], redact(e))
                from .scoped_accounting import deal_funding
                scoped_income = deal_funding(con, d)
                income = scoped_income if scoped_income is not None else [dict(r) for r in con.execute(
                    "SELECT income FROM funding_income WHERE venue=? AND symbol=? AND ts>=?",
                    (legs.perp.venue, d["symbol"], start))]
                pr = getattr(legs.perp, "position_risk", None)
                if pr is not None:
                    try:
                        row = next((r for r in pr(d["symbol"]) if r.get("symbol") == d["symbol"]), None)
                        lq, mk = dget((row or {}).get("liquidationPrice")), dget((row or {}).get("markPrice"))
                        if lq and mk:
                            liq = abs(mk - lq) / mk * 100
                    except Exception:          # noqa
                        pass
        sim = bool(d["sim"])
        # «PnL сейчас · при выходе»: последний расчёт трейдера (моложе MARK_S) или свежий той же функцией (marks);
        # сделку, которую ведёт исполнитель, не оцениваем заново. Фандинг только что добран — второй раз не читаем.
        pm = None if sol else marks.for_positions(con, d, legs, now=now,
                                                  fresh=resolve and d["id"] != busy_deal and legs is not None,
                                                  px_ask=spot_px)
        spot_units = bk.tokens_raw if sim else chk.wallet_units
        pos = (-bk.short if bk.short is not None else None) if sim else chk.position
        num = report.positions_numbers(spot_units=spot_units, dec_token=dec, spot_px=spot_px, position_amt=pos,
                                       mark=mark, unrealized=None, income_rows=income, created=float(d["created"]),
                                       now=now, book_tokens=bk.tokens(dec), book_short=bk.short, m=bk.m)
        from . import accounting
        if accounting.is_bound(con, d['id']):
            num['funding_usd'] = accounting.sources(con, d['id'], until_ms=int(now * 1000)).funding
        m_unknown = not getattr(bk, "m_known", True)
        if m_unknown:                                  # ревью 13.09, M3: дельта по m-заглушке была бы ложной голой ногой
            num["delta"] = num["delta_usd"] = None
        st = _step(legs, d["symbol"]) if legs is not None else None
        tstep = None if st is None else bk.tstep(st)    # строка «позиций» показывает только токены (дельта ног)
        upnl = _upnl(con, d["id"], bk.short, mark)
        # «до окупаемости ~N» — та же формула, что в плане и итоге (report.payback_h): вход + выход − получено
        usd_h = (rate / _period_h(con, d["id"]) * num["short_usd"]) if (rate is not None
                                                                         and num["short_usd"] is not None) else None
        cost, exit_est = _entry_costs(con, d["id"])
        payback = report.payback_h(cost, exit_est, usd_h, ZERO if sim else num["funding_usd"])
        rows.append(dict(deal_id=d["id"], coin=d["coin"], state=d["state"], chain=d["chain"],
                         perp_venue=d["perp_venue"], spot_qty=num["spot_tokens"], spot_usd=num["spot_usd"],
                         perp_qty=pos, perp_usd=num["short_usd"], upnl_usd=upnl, delta_qty=num["delta"],
                         delta_usd=num["delta_usd"],        # голая нога в $ — видна в «позициях» (C: всегда видно)
                         funding_usd=None if sim else num["funding_usd"], funding_n=None if sim else num["funding_count"],
                         held_s=max(now - float(d["created"]), 0.0), liq_dist_pct=liq,
                         liq_alert_pct=_liq_alert(con, d["id"]), reason=d["reason"], sim=sim,
                         step=tstep, leg_usd=dget(d["leg_usd"]),
                         payback_h=payback, pnl_now_usd=pm["pnl_now"] if pm else None,
                         pnl_exit_usd=pm["pnl_exit"] if pm else None,
                         pnl_uncovered=bool(pm and pm["flags"].get("uncovered")), m_unknown=m_unknown))
        verdicts.append(chk.matched)
        if chk.matched is not True:
            problems.append(f"{d['id']}: {chk.detail}")
    if not rows:
        return rows, None, None
    matched = False if False in verdicts else (True if all(v is True for v in verdicts) else None)
    return rows, matched, ("; ".join(problems) or None)


def _sol_mark(con, d: dict, legs, *, now: float, fresh: bool) -> dict | None:
    """«PnL сейчас · при выходе» сделки связки: оценка трейдера моложе MARK_S или свежая (sol_ledger, пишется в
    deal_marks); идёт исполнение — только таблица, не старше MARK_STALE_S."""
    from . import sol_ledger
    last = marks.decode(store.last_mark(con, d["id"]))
    recent = last if (last is not None and not last["flags"].get("final")
                      and now - last["ts"] < tconfig.MARK_STALE_S) else None
    if recent is not None and (now - recent["ts"] < tconfig.MARK_S or not fresh):
        return recent
    if not fresh or legs is None:
        return recent
    try:
        m = sol_ledger.mark(con, d, legs, now=now, cfg=marks.sol_cfg(d))
        marks.save(con, m)
    except Exception as e:                     # noqa — «позиции» без строки PnL, но не без позиций
        log.warning("оценка %s для «позиций»: %s", d["id"], redact(e))
        return recent
    return m.as_dict()


def _sol_row(con, d: dict, legs, chk: DealCheck, *, now: float, fresh: bool) -> dict:
    """Строка «позиций» сделки связки: спот — книга СДЕЛКИ (свои токены владельца в кошельке — не её), шорт — позиция HL
    (симуляция — книга), фандинг — фактические userFunding окна сделки, PnL и ликвидация — из оценки sol_ledger."""
    from . import sol_ledger
    from .sol_flow import _step
    bk = chk.book
    dec = int(d["token_dec"])
    sim = bool(d["sim"])
    pm = _sol_mark(con, d, legs, now=now, fresh=fresh)
    inst = sol_ledger.inst_of(d)
    try:
        m = D(str(inst.get("units_per_contract") or 1)) / D(str(inst.get("spot_units_per_token") or 1))
    except (ArithmeticError, ValueError):
        m = D(1)
    mark_px = rate = None
    step = None
    if legs is not None:
        try:
            fr = legs.perp.funding(d["symbol"])
            mark_px, rate = fr[0], fr[1]
        except Exception:                      # noqa
            pass
        try:
            step = _step(legs.perp)
        except Exception:                      # noqa
            step = None
    spot_px = pm["px_dex"] if pm else None
    pos = (-bk.short if bk.short is not None else None) if sim else chk.position
    income = [] if sim else [{"income": str(v)} for _t, v in sol_ledger.funding_rows(con, d)]
    num = report.positions_numbers(spot_units=bk.tokens_raw if bk.known else None, dec_token=dec, spot_px=spot_px,
                                   position_amt=pos, mark=mark_px, unrealized=None, income_rows=income,
                                   created=float(d["created"]), now=now, book_tokens=bk.tokens(dec) if bk.known else None,
                                   book_short=bk.short, m=m)
    liq = None
    lq = (pm or {}).get("flags", {}).get("liq") if pm else None
    if isinstance(lq, dict):
        dist = marks.liq_distance(lq.get("price"), lq.get("mark"))
        liq = dist * 100 if dist is not None else None
    usd_h = (rate / _period_h(con, d["id"]) * num["short_usd"]) if (rate is not None
                                                                     and num["short_usd"] is not None) else None
    cost, exit_est = _entry_costs(con, d["id"])
    payback = report.payback_h(cost, exit_est, usd_h, ZERO if sim else num["funding_usd"])
    return dict(deal_id=d["id"], coin=d["coin"], state=d["state"], chain=d["chain"], perp_venue=d["perp_venue"],
                spot_qty=num["spot_tokens"], spot_usd=num["spot_usd"], perp_qty=pos, perp_usd=num["short_usd"],
                upnl_usd=_upnl(con, d["id"], bk.short, mark_px), delta_qty=num["delta"], delta_usd=num["delta_usd"],
                funding_usd=None if sim else num["funding_usd"], funding_n=None if sim else num["funding_count"],
                held_s=max(now - float(d["created"]), 0.0), liq_dist_pct=liq, liq_alert_pct=None, reason=d["reason"],
                sim=sim, step=None if step is None else step * m, leg_usd=None, payback_h=payback,   # бюджет USDC ≠ «$ на ногу»
                pnl_now_usd=pm["pnl_now"] if pm else None, pnl_exit_usd=pm["pnl_exit"] if pm else None,
                pnl_uncovered=bool(pm and pm["flags"].get("uncovered")), m_unknown=False)


def _period_h(con, deal_id: str) -> D:
    """Период фандинга сделки, ч (из спецификации входа); нет — 1 ч, как у Desk._period."""
    r = con.execute("SELECT spec_json FROM intents WHERE deal_id=? AND kind='entry' ORDER BY created LIMIT 1",
                    (deal_id,)).fetchone()
    try:
        p = dget((json.loads(r[0]) if r and r[0] else {}).get("period_h"))
    except (TypeError, ValueError, AttributeError):
        p = None
    return p if (p is not None and p > 0) else D(1)


def _entry_costs(con, deal_id: str) -> tuple[D | None, D | None]:
    """Издержки входов сделки (факт — из итога) и оценка выхода (= издержки входов по плану, как в плане и итоге).
    Чего-то нет (итог без издержек, план без оценки) — (None, None): окупаемость тогда «—», а не выдуманная."""
    from .accounting import event_cost
    cost = plan = None
    for js, pj, iid in con.execute("SELECT e.json, i.plan_json, i.id FROM exec_events e JOIN intents i ON i.id = e.intent_id "
                              "WHERE e.deal_id=? AND e.kind='final' AND i.kind='entry'", (deal_id,)):
        try:
            c = event_cost(con, deal_id, iid, json.loads(js) if js else {})
            p = dget(((json.loads(pj) if pj else {}).get("est") or {}).get("total_usd"))
        except (TypeError, ValueError, AttributeError):
            return None, None
        if c is None or p is None:
            return None, None
        cost, plan = (cost or ZERO) + c, (plan or ZERO) + p
    return cost, plan


def _liq_alert(con, deal_id: str) -> D | None:
    """Порог тревоги «до ликвидации меньше, %», замороженный на входе (событие final входа): число владельца или
    «auto» = ½ расстояния на входе. Нет — None (симуляция, позиция на входе не прочитана)."""
    for (js,) in con.execute("SELECT json FROM exec_events WHERE deal_id=? AND kind='final' ORDER BY ts DESC, rowid DESC",
                             (deal_id,)):
        try:
            v = (json.loads(js) if js else {}).get("liq_alert_pct")
        except (TypeError, ValueError, AttributeError):
            continue
        if v is not None:
            return dget(v)
    return None


def _upnl(con, deal_id: str, short: D | None, mark: D | None) -> D | None:
    """PnL шорта по журналу заявок: продано − откуплено − шорт × марк (без комиссий и фандинга)."""
    if short is None or mark is None:
        return None
    net = perp_quote_flows(con, deal_id).net
    return None if net is None else net - short * mark



# --- проверки «статус» / trade-check ------------------------------------------------------------------------
def health_checks(rt, cfg, symbol: str | None = None) -> list[tuple[str, bool | None, str]]:
    """Шаг 2 выката (readonly): подписанные ЧТЕНИЯ — баланс Aster (доказывает вариант с user), one-way режим,
    комиссия, leverageBracket, часы; сеть BSC и ключ OKX. Ничего не отправляет."""
    out: list[tuple[str, bool | None, str]] = []

    def add(what: str, ok: bool | None, det: str = "") -> None:
        out.append((what, ok, det))

    def short(a: str | None) -> str:
        a = str(a or "")
        return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else (a or "—")

    k = getattr(rt, "keys", None)
    if k is None:
        add("ключи", None, "не загружены (режим dry)")
    else:
        add("адреса из ключей = owner.toml", True, f"EVM {short(k.evm_address)}; агент Aster {short(k.aster_signer)} "
                                                   f"≠ основной {short(k.aster_user)}")
    live = getattr(rt, "live", None)
    perp = getattr(live, "perp", None)
    if k is not None and perp is not None and hasattr(perp, "balances"):
        try:
            rows = perp.balances()
            usdt = next((r for r in rows if isinstance(r, dict) and r.get("asset") == "USDT"), None)
            add("Aster: подписанный баланс (вариант с user)", True,
                f"USDT доступно {_v().num(usdt.get('availableBalance')) if usdt else '—'}")
        except Exception as e:                 # noqa
            add("Aster: подписанный баланс (вариант с user)", False, redact(e)[:160])
        try:
            add("Aster: one-way режим", not perp.dual_side(), "")
        except Exception as e:                 # noqa
            add("Aster: one-way режим", None, redact(e)[:160])
        if hasattr(perp, "multi_assets"):
            try:
                ma, mt = perp.multi_assets(), cfg.get("perp.aster.margin_type")
                add("Aster: режим активов", not (ma and mt == "ISOLATED"),
                    ("Multi-Assets — ISOLATED из owner.toml не встанет (-4168): Futures → настройки → Asset Mode → "
                     "Single-Asset") if (ma and mt == "ISOLATED") else ("Multi-Assets" if ma else "Single-Asset"))
            except Exception as e:             # noqa
                add("Aster: режим активов", None, redact(e)[:160])
        if symbol:
            try:
                c = perp.commission_rate(symbol)
                rate = lambda k: _v().pct(None if dget(c.get(k)) is None else dget(c.get(k)) * 100, 3)
                add(f"Aster: комиссия {symbol}", True, f"мейкер {rate('makerCommissionRate')}, тейкер "
                                                      f"{rate('takerCommissionRate')}")
            except Exception as e:             # noqa
                add(f"Aster: комиссия {symbol}", None, redact(e)[:160])
            try:
                br = perp.leverage_bracket(symbol)
                br = br if isinstance(br, list) else [br]
                caps = [int(b.get("initialLeverage")) for r in br if isinstance(r, dict)
                        for b in (r.get("brackets") or []) if b.get("initialLeverage") is not None]
                lev = cfg.get("perp.aster.leverage")
                mx = max(caps) if caps else None
                add(f"Aster: leverageBracket {symbol}", None if (mx is None or lev is None) else lev <= mx,
                    f"максимум {mx}x, в owner.toml {lev if lev is not None else '—'}x")
            except Exception as e:             # noqa
                add(f"Aster: leverageBracket {symbol}", None, redact(e)[:160])
    # часы — публичный /fapi/v3/time: в dry — через публичную ногу под симуляцией
    clock = perp if perp is not None else getattr(getattr(getattr(rt, "sim", None), "perp", None), "inner", None)
    if clock is not None and hasattr(clock, "clock_offset_s"):
        try:
            off = clock.clock_offset_s()
            add("часы против Aster", abs(off) <= tconfig.ASTER_CLOCK_SKEW_MAX_S, f"{_v().num(off, 2, sign=True)} с")
        except Exception as e:                 # noqa
            add("часы против Aster", None, redact(e)[:160])
    legs = live or getattr(rt, "sim", None)
    spot = getattr(legs, "spot", None)
    base = getattr(spot, "inner", spot)
    rpc = getattr(base, "rpc", None)
    if rpc is not None:
        try:
            cid = rpc.chain_id()
            add("BSC RPC", cid == tconfig.CHAIN_IDS["bsc"], f"chainId {cid}")
        except Exception as e:                 # noqa
            add("BSC RPC", False, redact(e)[:160])
    okx = getattr(base, "okx", None)
    if okx is not None and hasattr(okx, "enabled"):
        add("ключ OKX DEX", bool(okx.enabled), "" if okx.enabled else "нет OKX_DEX_* в окружении")
    return out


def check_report(mode: str = "readonly", symbol: str | None = "AIW3USDT", *, environ=None, owner_path=None,
                 db_path=None, runtime=None) -> tuple[int, str]:
    """`funding_bot trade-check`: owner.toml, чего не хватает для live, ключи (readonly — адреса), подписанные
    чтения. Режим — не выше файла; live не берётся никогда (отправок нет, EVM-ключ не нужен). Код: 0 — всё ок,
    1 — есть ✗, 2 — не собрано (owner.toml или ключи)."""
    from . import owner as owner_mod
    from .engine import Conns, build_runtime
    from .keys import KeysError, effective_mode
    from .owner import OwnerConfigError
    try:
        cfg = owner_mod.load(owner_path)
    except OwnerConfigError as e:
        return 2, f"✗ owner.toml: {e}"
    lines = [f"owner.toml: {cfg.path} ({'sha256 ' + cfg.sha256[:12] if cfg.sha256 else 'файла нет — всё пусто'}) · "
             f"режим файла {cfg.mode}"]
    miss = cfg.live_missing("aster", "bsc")
    lines.append("для live не задано: " + (", ".join(miss) if miss else "всё задано"))
    m = effective_mode(cfg.mode, "readonly" if mode not in ("dry", "readonly") else mode)
    lines.append(f"режим проверки: {m}")
    try:
        rt = runtime or build_runtime(cfg, Conns(db_path), mode=m, environ=environ)
    except KeysError as e:
        return 2, "\n".join(lines + [f"✗ ключи: {e}"])
    bad = 0
    for what, ok, det in health_checks(rt, cfg, symbol):
        lines.append(f"{'✓' if ok is True else ('✗' if ok is False else '?')} {what}" + (f": {det}" if det else ""))
        bad += ok is False
    return (1 if bad else 0), "\n".join(lines)
