"""Исход отправленной транзакции Solana (ТЗ §9.3, SOLANA_ROUTERS §7; X03–X05, G08).

Правила:
  - статус ищется у КАЖДОГО узла отдельно (getSignatureStatuses, searchTransactionHistory=true): null одного
    узла ничего не доказывает;
  - найдена (confirmed/finalized) — LANDED_OK/LANDED_ERR по err; чек getTransaction ищется у всех узлов;
    чека нет — исход известен, суммы нет (amounts_known=False, UNKNOWN_AMOUNT: хедж по котировке нельзя);
    видна только processed — UNKNOWN (блок может откатиться);
  - узлы расходятся (err у одного, успех у другого; статус и чек разные) — UNKNOWN с доказательствами;
  - EXPIRED_NOT_LANDED — только всё сразу: lastValidBlockHeight из ТОГО ЖЕ ответа, что blockhash подписанного
    сообщения (lvbh_exact; чужая высота к готовому OKX tx не прикрепляется — G08), провайдер не держит
    неразрешённую отправку, не меньше двух независимых узлов, у каждого ФИНАЛИЗИРОВАННАЯ высота > lvbh
    (все блоки, куда транзакция могла попасть, финализированы), история каждого узла покрывает слот blockhash,
    и ПОСЛЕ этого повторный опрос статусов и getTransaction(finalized) у всех узлов пуст;
  - всё прочее — UNKNOWN. Таймер, слот вместо высоты и isBlockhashValid=false доказательством не являются
    (последнее пишется в evidence как справка).
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Sequence
from .rpc import GenesisMismatch, RpcError, RpcUnavailable

_ERRS = (RpcUnavailable, RpcError, GenesisMismatch)
_RANK = {"processed": 0, "confirmed": 1, "finalized": 2}


class Outcome(StrEnum):
    LANDED_OK = "LANDED_OK"
    LANDED_ERR = "LANDED_ERR"
    EXPIRED_NOT_LANDED = "EXPIRED_NOT_LANDED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AttemptRef:
    signature: str
    recent_blockhash: str
    last_valid_block_height: int | None
    lvbh_exact: bool                  # lvbh из того же ответа getLatestBlockhash, что и blockhash
    blockhash_slot: int | None        # слот контекста того ответа
    provider_pending: bool = False    # провайдер (managed execute) ещё может отправить — истечение не доказать


@dataclass(frozen=True)
class Resolution:
    outcome: Outcome
    commitment: str | None
    slot: int | None
    err: Any
    tx: dict | None = field(repr=False)   # сырой getTransaction(json), если найден
    reason: str = ""
    evidence: dict = field(default_factory=dict, repr=False)

    @property
    def amounts_known(self) -> bool:
        return self.tx is not None

    @property
    def final(self) -> bool:
        return self.commitment == "finalized"


class _Fail:
    def __init__(self, e: Exception):
        self.text = f"{type(e).__name__}: {e}"


def _js(v: Any) -> Any:
    return v.text if isinstance(v, _Fail) else v


def _key(err: Any) -> str:
    return json.dumps(err, sort_keys=True)


def _statuses(sig: str, endpoints: Sequence) -> dict[str, Any]:
    out = {}
    for ep in endpoints:
        try:
            vals, _ = ep.signature_statuses([sig], search_history=True)
            out[ep.label] = vals[0]
        except _ERRS as e:
            out[ep.label] = _Fail(e)
    return out


def _unknown(reason: str, ev: dict) -> Resolution:
    return Resolution(Outcome.UNKNOWN, None, None, None, None, reason, ev)


def _landed(att: AttemptRef, endpoints: Sequence, landed: dict[str, dict], ev: dict,
            receipt_commitment: str) -> Resolution:
    if len({_key(st.get("err")) for st in landed.values()}) > 1:
        return _unknown("RPC расходятся в исходе (err) — нужна ручная сверка", ev)
    best = max(landed.values(), key=lambda st: _RANK.get(st.get("confirmationStatus"), -1))
    commitment = best.get("confirmationStatus")
    if _RANK.get(commitment, -1) < _RANK["confirmed"]:
        return _unknown("видна только processed — блок может откатиться", ev)
    err, slot = best.get("err"), best.get("slot")
    want = "finalized" if commitment == "finalized" else receipt_commitment
    tx, tried = None, {}
    for ep in endpoints:
        try:
            got = ep.transaction(att.signature, commitment=want)
        except _ERRS as e:
            tried[ep.label] = _Fail(e).text
            continue
        tried[ep.label] = "found" if got else None
        if got:
            tx = got
            break
    ev["receipt_lookup"] = tried
    if tx is not None:
        meta = tx.get("meta") or {}
        if _key(meta.get("err")) != _key(err):
            return _unknown("чек и статус расходятся в err", ev)
        if tx.get("slot") != slot:
            return _unknown(f"слот чека {tx.get('slot')} ≠ слоту статуса {slot} — форк?", ev)
    outcome = Outcome.LANDED_ERR if err is not None else Outcome.LANDED_OK
    reason = "транзакция в сети" + ("" if tx is not None else "; чека нет — суммы неизвестны (UNKNOWN_AMOUNT)")
    return Resolution(outcome, commitment, slot, err, tx, reason, ev)


def resolve(att: AttemptRef, endpoints: Sequence, *, min_sources: int = 2,
            receipt_commitment: str = "confirmed") -> Resolution:
    """endpoints — независимые узлы (rpc.RpcPool.endpoints). Сетевые сбои → UNKNOWN, не исключение."""
    ev: dict[str, Any] = {"signature": att.signature, "blockhash": att.recent_blockhash,
                          "lvbh": att.last_valid_block_height, "lvbh_exact": att.lvbh_exact,
                          "blockhash_slot": att.blockhash_slot, "sources": [ep.label for ep in endpoints]}
    st_a = _statuses(att.signature, endpoints)
    ev["status_a"] = {k: _js(v) for k, v in st_a.items()}
    landed = {k: v for k, v in st_a.items() if isinstance(v, dict)}
    if landed:
        return _landed(att, endpoints, landed, ev, receipt_commitment)
    fails = [k for k, v in st_a.items() if isinstance(v, _Fail)]
    if fails:
        return _unknown(f"не ответили: {', '.join(fails)} — отсутствие не доказано", ev)
    if len(endpoints) < max(2, min_sources):
        return _unknown(f"истечение доказывается историей ≥{max(2, min_sources)} независимых RPC, есть "
                        f"{len(endpoints)}", ev)
    if att.provider_pending:
        return _unknown("провайдер ещё может отправить (requestId не разрешён)", ev)
    if att.last_valid_block_height is None or not att.lvbh_exact:
        valid = {}
        for ep in endpoints:
            try:
                valid[ep.label] = ep.is_blockhash_valid(att.recent_blockhash)[0]
            except _ERRS as e:
                valid[ep.label] = _Fail(e).text
        ev["is_blockhash_valid"] = valid                    # справка: false не доказывает неисполнения
        return _unknown("срок blockhash неизвестен: lastValidBlockHeight не из того же ответа, что hash (G08)", ev)
    if att.blockhash_slot is None:
        return _unknown("нет слота blockhash — покрытие истории узлов не доказать", ev)
    lvbh = att.last_valid_block_height
    heights: dict[str, Any] = {}
    for ep in endpoints:
        try:
            heights[ep.label] = ep.block_height("finalized")
        except _ERRS as e:
            heights[ep.label] = _Fail(e).text
    ev["finalized_height"] = heights
    if any(not isinstance(h, int) for h in heights.values()):
        return _unknown("высота блока известна не у всех RPC", ev)
    if any(h <= lvbh for h in heights.values()):
        return _unknown(f"blockhash ещё может войти в блок: финализированная высота {min(heights.values())} ≤ "
                        f"{lvbh}", ev)
    ledger: dict[str, Any] = {}
    for ep in endpoints:
        try:
            ledger[ep.label] = ep.minimum_ledger_slot()
        except _ERRS as e:
            ledger[ep.label] = _Fail(e).text
    ev["minimum_ledger_slot"] = ledger
    if any(not isinstance(s, int) for s in ledger.values()):
        return _unknown("начало истории известно не у всех RPC", ev)
    if any(s > att.blockhash_slot for s in ledger.values()):
        return _unknown("история узла начинается позже слота blockhash — отсутствие не доказать", ev)
    st_b = _statuses(att.signature, endpoints)            # после высоты: все возможные блоки уже финализированы
    ev["status_b"] = {k: _js(v) for k, v in st_b.items()}
    landed = {k: v for k, v in st_b.items() if isinstance(v, dict)}
    if landed:
        return _landed(att, endpoints, landed, ev, receipt_commitment)
    if any(isinstance(v, _Fail) for v in st_b.values()):
        return _unknown("повторный опрос статусов ответили не все RPC", ev)
    txs: dict[str, Any] = {}
    for ep in endpoints:
        try:
            txs[ep.label] = "found" if ep.transaction(att.signature, commitment="finalized") else None
        except _ERRS as e:
            txs[ep.label] = _Fail(e).text
    ev["tx_finalized"] = txs
    if any(v is not None for v in txs.values()):
        return _unknown("getTransaction и статусы расходятся (или не все ответили)", ev)
    return Resolution(Outcome.EXPIRED_NOT_LANDED, None, None, None, None,
                      f"не исполнена: финализированная высота > {lvbh} у всех RPC, история пуста", ev)
