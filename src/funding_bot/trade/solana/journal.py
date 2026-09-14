"""Журнал попыток Solana: sol_tx_attempts, свидетельства резолвера, чеки (ТЗ §9.2, §12; SOLANA_ROUTERS §7).

DDL — в store.py (SOL_JOURNAL_SCHEMA: миграция trade.db схемы 2 с воротами версии); ensure_schema — для отдельной
БД и сверки состояний этого модуля с частичными индексами store.
Работает на том же соединении SQLite, что store (WAL, synchronous=FULL): запись-до обязана пережить сбой ОС.

Протокол одной попытки:
  1. new_attempt      — VALIDATED: закреплены message_hash, blockhash, lastValidBlockHeight и точность пары,
                        провайдер/путь/requestId, логическое действие и предшественник;
  2. record_signed    — SIGNED_DURABLE: подписанные байты и первая подпись. Байты сверяются с закреплённым
                        (тот же хеш сообщения и blockhash, плательщик = кошелёк, все подписи верны). Исключение
                        здесь — отправлять НЕЛЬЗЯ (X01);
  3. mark_broadcast   — BROADCAST_ATTEMPTED до вызова sendTransaction; ответ узла — свидетельство, не исход;
  4. apply_resolution — исход только от резолвера: CONFIRMED_*/FINALIZED_*/EXPIRED_NOT_LANDED/UNKNOWN;
                        доказанное неисполнение после CONFIRMED — ROLLED_BACK (откат: компенсация — движок, G01/G02);
  5. record_receipt   — один чек на (сеть, подпись, логическая нога): повтор и finalized после confirmed второго
                        исполнения не создают (G03, X03); другой слот/суммы у той же подписи — ReceiptConflict.
                        Чек принимается только у попытки в сети (CONFIRMED_*/FINALIZED_*), с тем же ok и уровнем
                        не выше состояния; иначе отказ со свидетельством (исполнение не применять).
Попытка в сети без чека (резолвер нашёл статус, getTransaction ещё пуст — UNKNOWN_AMOUNT) остаётся в unresolved(),
пока чек не записан и не стал finalized: иначе купленное не найти при восстановлении (§6 п.6, X03).
Ограничения — индексами и триггерами, чтобы их не обошла ошибка движка:
  - одна незавершённая попытка на логическое действие;
  - одна подписанная неразрешённая попытка на (сеть, кошелёк): после UNKNOWN новый маршрут не подписать (R12);
  - новая попытка того же действия — только после доказанного неисполнения всех прежних (EXPIRED_NOT_LANDED,
    ABANDONED_UNSIGNED, ROLLED_BACK) или окончательно упавшей в сети (FINALIZED_ERR); после успеха — никогда;
  - закреплённые поля и подписанные байты не переписываются; свидетельства и факты чека — только вставка.
Подписанные байты — BLOB в этой же БД (атомарно с состоянием); наружу — только payload_for_retransmit().
Суммы — int/Decimal строкой; float в журнал не пишется (TypeError).
"""
from __future__ import annotations
import hashlib, json, re, secrets, sqlite3, time
from decimal import Decimal
from enum import StrEnum
from typing import Any, Mapping
from ..store import SOL_JOURNAL_SCHEMA, SOL_TX_INFLIGHT_STATES, SOL_TX_OPEN_STATES, ensure_journal_tables, tx
from . import wire
from .b58 import check_pubkey, signature_bytes
from .receipt import NativeFlows, Receipt, SwapAmounts
from .resolver import AttemptRef, Outcome, Resolution

SCHEMA_VERSION = 1


class AttemptState(StrEnum):
    VALIDATED = "VALIDATED"                     # сообщение проверено и закреплено, не подписано
    SIGNED_DURABLE = "SIGNED_DURABLE"           # подписанные байты в журнале; могли уйти (упали после send)
    BROADCAST_ATTEMPTED = "BROADCAST_ATTEMPTED"
    UNKNOWN = "UNKNOWN"
    CONFIRMED_OK = "CONFIRMED_OK"
    CONFIRMED_ERR = "CONFIRMED_ERR"
    FINALIZED_OK = "FINALIZED_OK"
    FINALIZED_ERR = "FINALIZED_ERR"
    EXPIRED_NOT_LANDED = "EXPIRED_NOT_LANDED"
    ROLLED_BACK = "ROLLED_BACK"                 # был confirmed, доказано: в канонической цепи нет
    ABANDONED_UNSIGNED = "ABANDONED_UNSIGNED"   # так и не подписана — в сеть уйти не могла


S = AttemptState
INFLIGHT = frozenset({S.SIGNED_DURABLE, S.BROADCAST_ATTEMPTED, S.UNKNOWN})
OPEN = INFLIGHT | {S.VALIDATED, S.CONFIRMED_OK, S.CONFIRMED_ERR}
TERMINAL = frozenset({S.FINALIZED_OK, S.FINALIZED_ERR, S.EXPIRED_NOT_LANDED, S.ROLLED_BACK, S.ABANDONED_UNSIGNED})
REPLACEABLE = frozenset({S.EXPIRED_NOT_LANDED, S.ABANDONED_UNSIGNED, S.ROLLED_BACK, S.FINALIZED_ERR})
FINALIZED = frozenset({S.FINALIZED_OK, S.FINALIZED_ERR})
_LANDED = frozenset({S.CONFIRMED_OK, S.CONFIRMED_ERR}) | FINALIZED
_FROM_INFLIGHT = {S.BROADCAST_ATTEMPTED, S.UNKNOWN, S.CONFIRMED_OK, S.CONFIRMED_ERR, S.FINALIZED_OK, S.FINALIZED_ERR,
                  S.EXPIRED_NOT_LANDED}
NEXT: dict[str, frozenset] = {
    S.VALIDATED: frozenset({S.SIGNED_DURABLE, S.ABANDONED_UNSIGNED}),
    S.SIGNED_DURABLE: frozenset(_FROM_INFLIGHT),
    S.BROADCAST_ATTEMPTED: frozenset(_FROM_INFLIGHT),
    S.UNKNOWN: frozenset(_FROM_INFLIGHT),
    S.CONFIRMED_OK: frozenset({S.FINALIZED_OK, S.ROLLED_BACK}),
    S.CONFIRMED_ERR: frozenset({S.FINALIZED_ERR, S.ROLLED_BACK}),
}


def _in(states) -> str:
    return "(" + ", ".join(f"'{s}'" for s in sorted(states)) + ")"


SCHEMA = SOL_JOURNAL_SCHEMA          # DDL живёт в store.py (одна схема trade.db и её версия)


class JournalError(RuntimeError):
    """Записать нельзя — и значит, внешнее действие не делать."""


class JournalBusy(JournalError):
    """Есть неразрешённая попытка того же действия или того же кошелька: новый маршрут/объём не подписывать."""


class BadTransition(JournalError):
    """Переход, которого нет в таблице NEXT: ошибка движка, а не «пропустить»."""


class ReceiptConflict(JournalError):
    """У подписи уже записан другой чек (слот/суммы) — откат/форк: нужна сверка, второго исполнения нет."""


# --- служебное -----------------------------------------------------------------------------------
def ensure_schema(con: sqlite3.Connection) -> None:
    """Идемпотентно. Журнал новее кода — отказ открыть (старый код не должен писать в новую схему); ворота версии
    trade.db — в store.ensure_journal_tables."""
    if {str(s) for s in INFLIGHT} != set(SOL_TX_INFLIGHT_STATES) or {str(s) for s in OPEN} != set(SOL_TX_OPEN_STATES):
        raise JournalError("состояния журнала и частичные индексы store разошлись — не открываю")
    ensure_journal_tables(con, "sol")
    row = con.execute("SELECT v FROM sol_meta WHERE k='schema'").fetchone()
    if row is None:
        con.execute("INSERT OR IGNORE INTO sol_meta(k, v) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
    elif int(row[0]) > SCHEMA_VERSION:
        raise JournalError(f"журнал Solana схемы {row[0]} новее кода ({SCHEMA_VERSION}) — не открываю")


def _plain(o: Any) -> Any:
    if isinstance(o, float):
        raise TypeError("float в журнал Solana не пишется: суммы — int или Decimal")
    if o is None or isinstance(o, (bool, int, str)):
        return o
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, Mapping):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    raise TypeError(f"{type(o).__name__} в журнал Solana не пишется")


def _jdump(o: Any) -> str:
    return json.dumps(_plain(o), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row(con, attempt_id: str, *, payload: bool = False) -> dict:
    cols = "*" if payload else ", ".join(c for c in _COLS if c != "signed_payload")
    r = con.execute(f"SELECT {cols} FROM sol_tx_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    if r is None:
        raise JournalError(f"попытки {attempt_id} нет")
    return dict(r) if isinstance(r, sqlite3.Row) else dict(zip(_names(con, payload), r))


_COLS = ("attempt_id", "network", "wallet", "logical_action_id", "predecessor_attempt_id", "clip_ref", "provider",
         "path", "request_id", "payload_kind", "message_hash", "recent_blockhash", "last_valid_block_height",
         "lvbh_exact", "blockhash_slot", "provider_pending", "plan_json", "signature", "signed_payload", "state",
         "outcome_json", "created", "updated")


def _names(con, payload: bool) -> tuple[str, ...]:
    return _COLS if payload else tuple(c for c in _COLS if c != "signed_payload")


def _evidence(con, attempt_id: str, kind: str, frm: str | None, to: str | None, data: Any, now: float) -> None:
    con.execute("INSERT INTO sol_tx_evidence(attempt_id, ts, kind, state_from, state_to, json) VALUES (?,?,?,?,?,?)",
                (attempt_id, now, kind, frm, to, None if data is None else _jdump(data)))


def _move(con, attempt_id: str, new: str, now: float, *, kind: str, data: Any = None, **fields) -> str:
    """Переход по таблице NEXT с проверкой текущего состояния в той же транзакции."""
    cur = _row(con, attempt_id)["state"]
    if new not in NEXT.get(cur, frozenset()):
        raise BadTransition(f"{attempt_id}: {cur} → {new} не допускается")
    sets = ", ".join(f"{k}=?" for k in fields)
    sql = f"UPDATE sol_tx_attempts SET state=?, updated=?{', ' + sets if sets else ''} WHERE attempt_id=? AND state=?"
    try:
        n = con.execute(sql, (new, now, *fields.values(), attempt_id, cur)).rowcount
    except sqlite3.IntegrityError as e:
        raise JournalBusy(f"{attempt_id}: {cur} → {new}: {e}") from None
    if n != 1:
        raise BadTransition(f"{attempt_id}: состояние сменилось параллельно")
    _evidence(con, attempt_id, kind, cur, new, data, now)
    return new


# --- протокол ------------------------------------------------------------------------------------
def new_attempt(con, *, network: str, wallet: str, logical_action_id: str, provider: str, path: str,
                payload_kind: str, message_hash: str, recent_blockhash: str, last_valid_block_height: int | None,
                lvbh_exact: bool, blockhash_slot: int | None, plan: Mapping, clip_ref: str | None = None,
                request_id: str | None = None, predecessor_attempt_id: str | None = None,
                provider_pending: bool = False, now: float | None = None) -> str:
    """VALIDATED. Прежние попытки того же действия обязаны быть доказанно не исполнены (REPLACEABLE)."""
    check_pubkey(network, "сеть (genesis)")
    check_pubkey(wallet, "кошелёк")
    check_pubkey(recent_blockhash, "blockhash")
    if not re.fullmatch(r"[0-9a-f]{64}", str(message_hash)):
        raise JournalError("message_hash: не sha256 hex")
    for v, what in ((last_valid_block_height, "lastValidBlockHeight"), (blockhash_slot, "blockhash_slot")):
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0):
            raise JournalError(f"{what}: не целое ≥ 0")
    if lvbh_exact and last_valid_block_height is None:
        raise JournalError("lvbh_exact без lastValidBlockHeight")
    if not logical_action_id or not provider or not path or not payload_kind:
        raise JournalError("пустое действие/провайдер/путь/вид payload")
    plan_json = _jdump(plan)
    now = time.time() if now is None else now
    aid = f"sa-{secrets.token_hex(8)}"
    with tx(con):
        from ..store import execution_paused
        if execution_paused(con):
            raise JournalError('new Solana action fenced by owner/deployment pause')
        prev = [dict(zip(("attempt_id", "state"), r)) for r in con.execute(
            "SELECT attempt_id, state FROM sol_tx_attempts WHERE logical_action_id=? ORDER BY created",
            (logical_action_id,))]
        for p in prev:
            if p["state"] in (S.CONFIRMED_OK, S.FINALIZED_OK):
                raise JournalError(f"действие {logical_action_id} уже исполнено попыткой {p['attempt_id']}")
            if p["state"] not in REPLACEABLE:
                raise JournalBusy(f"попытка {p['attempt_id']} в {p['state']}: сначала доказанный исход")
        if prev and predecessor_attempt_id not in {p["attempt_id"] for p in prev}:
            raise JournalError("повтор действия без ссылки на своего предшественника")
        if not prev and predecessor_attempt_id is not None:
            raise JournalError("предшественник указан, а прежних попыток действия нет")
        try:
            con.execute(
                "INSERT INTO sol_tx_attempts(attempt_id, network, wallet, logical_action_id, predecessor_attempt_id, "
                "clip_ref, provider, path, request_id, payload_kind, message_hash, recent_blockhash, "
                "last_valid_block_height, lvbh_exact, blockhash_slot, provider_pending, plan_json, state, created, "
                "updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (aid, network, wallet, logical_action_id, predecessor_attempt_id, clip_ref, provider, path, request_id,
                 payload_kind, message_hash, recent_blockhash, last_valid_block_height, int(bool(lvbh_exact)),
                 blockhash_slot, int(bool(provider_pending)), plan_json, S.VALIDATED, now, now))
        except sqlite3.IntegrityError as e:
            raise JournalBusy(f"новая попытка {logical_action_id}: {e}") from None
        _evidence(con, aid, "validated", None, S.VALIDATED, {"message_hash": message_hash}, now)
    return aid


def record_signed(con, attempt_id: str, signed: bytes, *, now: float | None = None) -> str:
    """SIGNED_DURABLE + байты. Любое исключение — отправлять нельзя (X01). Возвращает подпись (txid)."""
    now = time.time() if now is None else now
    row = _row(con, attempt_id)
    if row["state"] != S.VALIDATED:
        raise BadTransition(f"{attempt_id}: подписать можно только VALIDATED, сейчас {row['state']}")
    try:
        wtx = wire.parse_transaction(bytes(signed))
    except wire.WireError as e:
        raise JournalError(f"подписанные байты не разбираются: {e}") from None
    m = wtx.message
    if m.message_hash != row["message_hash"]:
        raise JournalError("подписано не то сообщение, что проверено (хеш не совпал)")
    if m.recent_blockhash != row["recent_blockhash"]:
        raise JournalError("blockhash подписанного сообщения не тот, что закреплён")
    if m.fee_payer != row["wallet"]:
        raise JournalError(f"плательщик {m.fee_payer}, а кошелёк попытки {row['wallet']}")
    if not wtx.fully_signed or not all(wtx.signature_ok()):
        raise JournalError("подпись пуста или не проходит проверку Ed25519")
    sig = wtx.signature
    with tx(con):
        _move(con, attempt_id, S.SIGNED_DURABLE, now, kind="signed", data={"signature": sig},
              signature=sig, signed_payload=bytes(signed))
    return sig


def abandon_unsigned(con, attempt_id: str, reason: str, *, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with tx(con):
        _move(con, attempt_id, S.ABANDONED_UNSIGNED, now, kind="abandoned", data={"reason": reason})


def mark_broadcast(con, attempt_id: str, *, endpoint: str, now: float | None = None) -> bytes:
    """Записать «отправляю» ДО sendTransaction и вернуть ТЕ ЖЕ байты (первая отправка и повтор)."""
    now = time.time() if now is None else now
    with tx(con):
        row = _row(con, attempt_id, payload=True)
        if row["state"] not in INFLIGHT or row["signed_payload"] is None:
            raise BadTransition(f"{attempt_id}: отправлять можно только подписанную неразрешённую, "
                                f"сейчас {row['state']}")
        _move(con, attempt_id, S.BROADCAST_ATTEMPTED, now, kind="broadcast", data={"endpoint": endpoint})
        return bytes(row["signed_payload"])


def record_send_result(con, attempt_id: str, *, result: str, detail: str = "", now: float | None = None) -> None:
    """Ответ узла на отправку — только свидетельство: accepted | rejected | no_response. Состояние не меняет."""
    if result not in ("accepted", "rejected", "no_response"):
        raise JournalError(f"результат отправки {result!r}")
    now = time.time() if now is None else now
    with tx(con):
        _row(con, attempt_id)
        _evidence(con, attempt_id, f"send_{result}", None, None, {"detail": str(detail)[:300]}, now)


def payload_for_retransmit(con, attempt_id: str) -> bytes:
    """Те же подписанные байты для повтора (X02). Только пока попытка в полёте и не разрешена."""
    row = _row(con, attempt_id, payload=True)
    if row["state"] not in INFLIGHT or row["signed_payload"] is None:
        raise BadTransition(f"{attempt_id}: повторять можно только подписанную неразрешённую, сейчас {row['state']}")
    return bytes(row["signed_payload"])


def _target(cur: str, res: Resolution) -> str | None:
    if res.outcome in (Outcome.LANDED_OK, Outcome.LANDED_ERR):
        ok = res.outcome == Outcome.LANDED_OK
        if res.commitment == "finalized":
            return S.FINALIZED_OK if ok else S.FINALIZED_ERR
        return S.CONFIRMED_OK if ok else S.CONFIRMED_ERR
    if res.outcome == Outcome.EXPIRED_NOT_LANDED:
        return S.ROLLED_BACK if cur in (S.CONFIRMED_OK, S.CONFIRMED_ERR) else S.EXPIRED_NOT_LANDED
    return S.UNKNOWN if cur in INFLIGHT else None      # UNKNOWN не понижает подтверждённое (RPC мог просто молчать)


def apply_resolution(con, attempt_id: str, res: Resolution, *, now: float | None = None) -> str:
    """Исход резолвера → состояние. Возвращает новое (или прежнее, если это лишь свидетельство)."""
    now = time.time() if now is None else now
    with tx(con):
        row = _row(con, attempt_id)
        cur = row["state"]
        if row["signature"] is None:
            raise BadTransition(f"{attempt_id}: не подписана — исход в сети невозможен")
        if cur in _LANDED and res.outcome in (Outcome.LANDED_OK, Outcome.LANDED_ERR) and \
                (cur in (S.CONFIRMED_OK, S.FINALIZED_OK)) != (res.outcome == Outcome.LANDED_OK):
            _evidence(con, attempt_id, "conflict", cur, None, {"reason": res.reason, **res.evidence}, now)
            raise JournalError(f"{attempt_id}: был {cur}, резолвер говорит {res.outcome} — ручная сверка")
        new = _target(cur, res)
        data = {"outcome": res.outcome, "commitment": res.commitment, "slot": res.slot, "reason": res.reason,
                "evidence": res.evidence}
        outcome_json = _jdump({"outcome": res.outcome, "commitment": res.commitment, "slot": res.slot,
                               "err": json.dumps(res.err, sort_keys=True), "reason": res.reason})
        if new is None or new == cur and cur != S.UNKNOWN:
            _evidence(con, attempt_id, "resolution", cur, None, data, now)
            return cur
        if new == cur:                                    # UNKNOWN → UNKNOWN: новое свидетельство
            con.execute("UPDATE sol_tx_attempts SET outcome_json=?, updated=? WHERE attempt_id=?",
                        (outcome_json, now, attempt_id))
            _evidence(con, attempt_id, "resolution", cur, cur, data, now)
            return cur
        return _move(con, attempt_id, new, now, kind="resolution", data=data, outcome_json=outcome_json)


def _flows(amounts: SwapAmounts | None, native: NativeFlows | None) -> dict:
    out: dict[str, Any] = {}
    if amounts is not None:
        out.update(ok=amounts.ok, in_mint=amounts.in_flow.mint, out_mint=amounts.out_flow.mint,
                   in_raw=str(amounts.in_raw), out_raw=str(amounts.out_raw),
                   in_accounts=[a.address for a in amounts.in_flow.accounts],
                   out_accounts=[a.address for a in amounts.out_flow.accounts])
    if native is not None:
        out.update(wallet=native.wallet, fee_paid_by_wallet=str(native.fee_paid_by_wallet),
                   rent_deposit=str(native.rent_deposit_lamports), rent_refund=str(native.rent_refund_lamports),
                   tip=str(native.tip_lamports), external=str(native.external_lamports),
                   wsol_delta=str(native.wsol_delta_raw))
    return out


def _receipt_mismatch(state: str, receipt: Receipt, commitment: str) -> str | None:
    """Почему чек нельзя принять у попытки в этом состоянии (None — можно)."""
    if state not in _LANDED:
        return f"чек только у попытки в сети, а она {state}"
    if receipt.ok != state.endswith("_OK"):
        return f"чек ok={receipt.ok}, а попытка {state} — ручная сверка"
    if commitment == "finalized" and state not in FINALIZED:
        return f"чек finalized, а попытка лишь {state}"
    return None


def record_receipt(con, *, attempt_id: str, logical_leg: str, receipt: Receipt, amounts: SwapAmounts | None,
                   native: NativeFlows | None, commitment: str, now: float | None = None) -> str:
    """"new" — первый раз (движок применяет исполнение в ТОЙ ЖЕ транзакции БД); "finality" — тот же чек стал
    finalized; "same" — повтор. Другой чек той же подписи — ReceiptConflict. Попытка не в сети, чек с другим ok
    или уровень выше состояния — JournalError: свидетельство фиксируется, исполнение не применять."""
    if commitment not in ("confirmed", "finalized"):
        raise JournalError(f"уровень чека {commitment!r}")
    if not logical_leg:
        raise JournalError("пустая логическая нога")
    now = time.time() if now is None else now
    flows = _flows(amounts, native)
    facts = {"slot": receipt.slot, "ok": receipt.ok, "err": json.dumps(receipt.err, sort_keys=True),
             "fee": str(receipt.fee_lamports), "flows": flows}
    rh = hashlib.sha256(_jdump(facts).encode()).hexdigest()
    with tx(con):
        row = _row(con, attempt_id)
        if row["signature"] != receipt.signature:
            raise JournalError(f"{attempt_id}: чек подписи {receipt.signature}, а попытка — {row['signature']}")
        bad = _receipt_mismatch(row["state"], receipt, commitment)   # проверка и запись — в одной транзакции
        if bad is None:
            return _put_receipt(con, row, logical_leg, receipt, commitment, facts, flows, rh, now)
        # отказ: свидетельство коммитится на выходе из tx, исключение — уже после (по F3);
        # во внешней транзакции движка судьба свидетельства — вместе с ней
        _evidence(con, attempt_id, "receipt_refused", row["state"], None,
                  {"leg": logical_leg, "commitment": commitment, "ok": receipt.ok, "receipt_hash": rh,
                   "reason": bad}, now)
    raise JournalError(f"{attempt_id}: {bad}")


def _put_receipt(con, row: Mapping, logical_leg: str, receipt: Receipt, commitment: str, facts: dict, flows: dict,
                 rh: str, now: float) -> str:
    attempt_id, network = row["attempt_id"], row["network"]
    ex = con.execute("SELECT receipt_hash, commitment FROM sol_receipts WHERE network=? AND signature=? AND "
                     "logical_leg=?", (network, receipt.signature, logical_leg)).fetchone()
    if ex is None:
        con.execute("INSERT INTO sol_receipts(network, signature, logical_leg, attempt_id, slot, block_time, ok, "
                    "err_json, fee_lamports, flows_json, receipt_hash, commitment, first_seen, updated) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (network, receipt.signature, logical_leg, attempt_id, receipt.slot, receipt.block_time,
                     int(receipt.ok), facts["err"], facts["fee"], _jdump(flows), rh, commitment, now, now))
        _evidence(con, attempt_id, "receipt", None, None, {"leg": logical_leg, "commitment": commitment,
                                                            "receipt_hash": rh}, now)
        return "new"
    old_hash, old_c = ex[0], ex[1]
    if old_hash != rh:
        _evidence(con, attempt_id, "receipt_conflict", None, None, {"leg": logical_leg, "old": old_hash,
                                                                     "new": rh}, now)
        raise ReceiptConflict(f"{receipt.signature}/{logical_leg}: чек изменился — откат или форк, нужна сверка")
    if old_c == "confirmed" and commitment == "finalized":
        con.execute("UPDATE sol_receipts SET commitment='finalized', updated=? WHERE network=? AND signature=? "
                    "AND logical_leg=?", (now, network, receipt.signature, logical_leg))
        _evidence(con, attempt_id, "receipt_finalized", None, None, {"leg": logical_leg}, now)
        return "finality"
    return "same"


# --- чтение --------------------------------------------------------------------------------------
def attempt(con, attempt_id: str) -> dict:
    """Строка попытки БЕЗ подписанных байтов (readonly API и логи их не видят)."""
    return _row(con, attempt_id)


def unresolved(con) -> list[dict]:
    """Для старта — до новых команд (X10): подписанные неразрешённые, подтверждённые без финальности и
    финализированные, у которых чек не записан или не стал finalized (UNKNOWN_AMOUNT: суммы ещё не применены).
    Цикл один: резолвер → apply_resolution → record_receipt, пока попытка отсюда не уйдёт."""
    cols = ", ".join(_names(con, False))
    states = INFLIGHT | {S.CONFIRMED_OK, S.CONFIRMED_ERR}
    return [dict(zip(_names(con, False), r)) for r in con.execute(
        f"SELECT {cols} FROM sol_tx_attempts a WHERE a.state IN {_in(states)} "
        f"OR (a.state IN {_in(FINALIZED)} AND (NOT EXISTS (SELECT 1 FROM sol_receipts r WHERE "
        f"r.network=a.network AND r.signature=a.signature) OR EXISTS (SELECT 1 FROM sol_receipts r WHERE "
        f"r.network=a.network AND r.signature=a.signature AND r.commitment<>'finalized'))) ORDER BY a.created")]


def evidence(con, attempt_id: str) -> list[dict]:
    return [dict(zip(("id", "ts", "kind", "state_from", "state_to", "json"), r)) for r in con.execute(
        "SELECT id, ts, kind, state_from, state_to, json FROM sol_tx_evidence WHERE attempt_id=? ORDER BY id",
        (attempt_id,))]


def attempt_ref(row: Mapping) -> AttemptRef:
    """Строка журнала → вход резолвера."""
    sig = row["signature"]
    if sig is None:
        raise JournalError("у попытки нет подписи")
    signature_bytes(sig)
    return AttemptRef(signature=sig, recent_blockhash=row["recent_blockhash"],
                      last_valid_block_height=row["last_valid_block_height"], lvbh_exact=bool(row["lvbh_exact"]),
                      blockhash_slot=row["blockhash_slot"], provider_pending=bool(row["provider_pending"]))
