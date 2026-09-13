"""Журнал попыток Solana на настоящей SQLite (store.connect: WAL, synchronous=FULL): запись-до (X01), рестарт
после записи — те же байты (X02), чек привязан к попытке и учитывается один раз (X03, G03), новая попытка только
после доказанного неисполнения (X05), одна неразрешённая попытка на кошелёк (R12), неизменность фактов.
Подписанные байты — реальная транзакция mainnet (jup_sell) и собственные на тестовом ключе."""
import base64, copy, hashlib, sqlite3
import pytest
from funding_bot.trade import store
from funding_bot.trade.solana import ANSEM_MINT, MAINNET_GENESIS, TOKEN_2022_PROGRAM, TOKEN_PROGRAM, USDC_MINT, wire
from funding_bot.trade.solana import journal as J
from funding_bot.trade.solana.b58 import Base58Error
from funding_bot.trade.solana.receipt import parse_receipt
from funding_bot.trade.solana.resolver import Outcome, Resolution, resolve
from funding_bot.trade.solana.sign import sign_validated
from solana_helpers import PcdSigner, fx, legacy_transfer

SELL_BYTES = base64.b64decode(fx("tx_jup_sell_b64.json")["result"]["transaction"][0])
SELL = wire.parse_transaction(SELL_BYTES)
SELL_JSON = fx("tx_jup_sell.json")["result"]
WALLET = SELL.message.fee_payer
PLAN = {"amount_in_raw": 51_882_704, "min_out_raw": 8_500_000, "in_mint": ANSEM_MINT, "out_mint": USDC_MINT}
EXPIRED = Resolution(Outcome.EXPIRED_NOT_LANDED, None, None, None, None, "истёк", {"proof": 1})
UNK = Resolution(Outcome.UNKNOWN, None, None, None, None, "молчат", {})
ERR_JSON = copy.deepcopy(SELL_JSON)                       # тот же txid, упавший в сети: комиссия есть, свопа нет
ERR_JSON["meta"]["err"] = {"InstructionError": [3, {"Custom": 6001}]}


def landed(commitment="confirmed", ok=True, tx=SELL_JSON):
    return Resolution(Outcome.LANDED_OK if ok else Outcome.LANDED_ERR, commitment, SELL_JSON["slot"],
                      None if ok else {"x": 1}, tx, "в сети", {"k": "v"})


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "trade.db"
    con = store.connect(p)
    J.ensure_schema(con)
    yield con, p
    con.close()


def new(con, *, action="clip-1", msg=SELL.message, wallet=WALLET, **kw):
    return J.new_attempt(con, network=MAINNET_GENESIS, wallet=wallet, logical_action_id=action, provider="jupiter",
                         path="jupiter_build_v2", payload_kind="tx", message_hash=msg.message_hash,
                         recent_blockhash=msg.recent_blockhash, last_valid_block_height=kw.pop("lvbh", 424_758_164),
                         lvbh_exact=kw.pop("exact", True), blockhash_slot=kw.pop("slot", 446_710_900),
                         plan=kw.pop("plan", PLAN), **kw)


def execute_once(con, aid, signed, send):
    """Контракт исполнителя (движок — после фазы 1): подпись в журнал → «отправляю» → send ТЕХ ЖЕ байт."""
    J.record_signed(con, aid, signed)
    payload = J.mark_broadcast(con, aid, endpoint="primary@rpc.test")
    return send(payload)


class FailingCon:
    """Соединение, у которого не проходит запись попытки (диск/блокировка)."""

    def __init__(self, con):
        self._c = con

    def __getattr__(self, k):
        return getattr(self._c, k)

    def execute(self, sql, *a):
        if sql.startswith("UPDATE sol_tx_attempts"):
            raise sqlite3.OperationalError("disk I/O error")
        return self._c.execute(sql, *a)


def own_signed(seed_tag: str, lamports: int):
    from funding_bot.trade.solana.b58 import pubkey_bytes
    signer = PcdSigner(hashlib.sha256(seed_tag.encode()).digest())
    m = legacy_transfer(pubkey_bytes(signer.public_key()), hashlib.sha256(b"dest").digest(), lamports,
                        SELL.message.recent_blockhash)
    return signer, m, sign_validated(signer, m, wire.message_hash(m))


# --- основной путь -------------------------------------------------------------------------------
def test_happy_path_on_real_mainnet_bytes(db):
    con, path = db
    aid = new(con)
    sent = []
    execute_once(con, aid, SELL_BYTES, sent.append)
    assert sent == [SELL_BYTES]
    row = J.attempt(con, aid)
    assert row["signature"] == SELL.signature and row["state"] == "BROADCAST_ATTEMPTED"
    assert "signed_payload" not in row                     # readonly-чтение подписанных байт не отдаёт
    assert J.apply_resolution(con, aid, landed()) == "CONFIRMED_OK"
    rc = parse_receipt(SELL_JSON, expect_signature=SELL.signature)
    amt = rc.swap_amounts(wallet=WALLET, in_mint=ANSEM_MINT, in_program=TOKEN_2022_PROGRAM, out_mint=USDC_MINT,
                          out_program=TOKEN_PROGRAM)
    rec = dict(attempt_id=aid, logical_leg="spot", receipt=rc, amounts=amt, native=rc.native(WALLET))
    assert J.record_receipt(con, **rec, commitment="confirmed") == "new"
    assert J.apply_resolution(con, aid, landed("finalized")) == "FINALIZED_OK"
    assert J.record_receipt(con, **rec, commitment="finalized") == "finality"          # G03
    con2 = store.connect(path)                                                          # рестарт
    J.ensure_schema(con2)
    assert J.record_receipt(con2, **rec, commitment="finalized") == "same"
    assert J.record_receipt(con2, **rec, commitment="confirmed") == "same"
    rows = con2.execute("SELECT flows_json, commitment FROM sol_receipts").fetchall()
    assert len(rows) == 1 and rows[0][1] == "finalized"
    assert '"in_raw":"51882704"' in rows[0][0] and '"out_raw":"8574400"' in rows[0][0]
    assert J.unresolved(con2) == []
    with pytest.raises(J.JournalError, match="уже исполнено"):
        new(con2, predecessor_attempt_id=aid)
    con2.close()


def test_x01_no_send_without_durable_record(db):
    con, _ = db
    aid = new(con)
    sent = []
    other = base64.b64decode(fx("tx_jup_buy_b64.json")["result"]["transaction"][0])
    with pytest.raises(J.JournalError, match="не то сообщение"):
        execute_once(con, aid, other, sent.append)
    bad_sig = bytearray(SELL_BYTES)
    bad_sig[1] ^= 1
    with pytest.raises(J.JournalError, match="подпись"):
        execute_once(con, aid, bytes(bad_sig), sent.append)
    with pytest.raises(J.JournalError):
        execute_once(con, aid, b"\x01garbage", sent.append)
    with pytest.raises(sqlite3.OperationalError):          # запись упала — «отправили, запишем потом» не бывает
        execute_once(FailingCon(con), aid, SELL_BYTES, sent.append)
    row = J.attempt(con, aid)
    assert sent == [] and row["state"] == "VALIDATED" and row["signature"] is None
    execute_once(con, aid, SELL_BYTES, sent.append)
    assert sent == [SELL_BYTES]


def test_x02_restart_after_durable_retransmits_same_bytes(db):
    con, path = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    con.close()                                            # упали до первой отправки
    con2 = store.connect(path)
    J.ensure_schema(con2)
    pend = J.unresolved(con2)
    assert [(r["attempt_id"], r["state"]) for r in pend] == [(aid, "SIGNED_DURABLE")]
    assert J.payload_for_retransmit(con2, aid) == SELL_BYTES
    assert J.mark_broadcast(con2, aid, endpoint="a") == SELL_BYTES
    assert J.mark_broadcast(con2, aid, endpoint="b") == SELL_BYTES      # повтор — те же байты, та же подпись
    with pytest.raises(J.JournalBusy):                     # новый blockhash/маршрут, пока исход первой не известен
        new(con2, predecessor_attempt_id=aid)
    ref = J.attempt_ref(J.attempt(con2, aid))
    assert (ref.signature, ref.lvbh_exact, ref.blockhash_slot, ref.provider_pending) == \
        (SELL.signature, True, 446_710_900, False)
    con2.close()


def test_r12_one_unresolved_attempt_per_wallet(db):
    con, _ = db
    signer, m1, s1 = own_signed("wallet-a", 1)
    _, m2, s2 = own_signed("wallet-a", 2)
    a1 = new(con, action="clip-1", msg=wire.parse_message(m1), wallet=signer.public_key())
    a2 = new(con, action="clip-2", msg=wire.parse_message(m2), wallet=signer.public_key())
    J.record_signed(con, a1, s1)
    J.mark_broadcast(con, a1, endpoint="a")
    assert J.apply_resolution(con, a1, UNK) == "UNKNOWN"
    with pytest.raises(J.JournalBusy):                     # второй своп того же кошелька не подписать поверх UNKNOWN
        J.record_signed(con, a2, s2)
    assert J.attempt(con, a2)["state"] == "VALIDATED"
    assert J.apply_resolution(con, a1, EXPIRED) == "EXPIRED_NOT_LANDED"
    assert J.record_signed(con, a2, s2) == wire.parse_transaction(s2).signature


def test_x05_replacement_only_after_proven_expiry(db):
    con, _ = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    J.mark_broadcast(con, aid, endpoint="a")
    J.record_send_result(con, aid, result="no_response", detail="Timeout")
    assert J.apply_resolution(con, aid, UNK) == "UNKNOWN"
    assert J.apply_resolution(con, aid, UNK) == "UNKNOWN"          # новое свидетельство, то же состояние
    with pytest.raises(J.JournalBusy):
        new(con, predecessor_attempt_id=aid)
    assert J.apply_resolution(con, aid, EXPIRED) == "EXPIRED_NOT_LANDED"
    with pytest.raises(J.BadTransition):
        J.payload_for_retransmit(con, aid)                 # истёкшую не повторяем
    with pytest.raises(J.JournalError, match="предшественник"):
        new(con)
    a2 = new(con, predecessor_attempt_id=aid)
    assert J.attempt(con, a2)["predecessor_attempt_id"] == aid
    kinds = [e["kind"] for e in J.evidence(con, aid)]
    assert kinds == ["validated", "signed", "broadcast", "send_no_response", "resolution", "resolution", "resolution"]


def test_confirmed_not_downgraded_by_silence_rollback_proven(db):
    con, _ = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    assert J.apply_resolution(con, aid, landed()) == "CONFIRMED_OK"
    assert J.apply_resolution(con, aid, UNK) == "CONFIRMED_OK"     # молчание RPC — не откат
    with pytest.raises(J.JournalError, match="ручная сверка"):
        J.apply_resolution(con, aid, landed(ok=False))
    assert J.apply_resolution(con, aid, EXPIRED) == "ROLLED_BACK"  # доказанное отсутствие после confirmed
    assert J.attempt(con, new(con, predecessor_attempt_id=aid))["state"] == "VALIDATED"


def test_receipt_conflict_and_immutable_facts(db):
    con, _ = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    J.apply_resolution(con, aid, landed())
    rc = parse_receipt(SELL_JSON)
    J.record_receipt(con, attempt_id=aid, logical_leg="spot", receipt=rc, amounts=None, native=None,
                     commitment="confirmed")
    moved = copy.deepcopy(SELL_JSON)
    moved["slot"] += 1
    with pytest.raises(J.ReceiptConflict):                 # тот же txid в другом слоте — форк, не второе исполнение
        J.record_receipt(con, attempt_id=aid, logical_leg="spot", receipt=parse_receipt(moved), amounts=None,
                         native=None, commitment="confirmed")
    with pytest.raises(J.JournalError, match="чек подписи"):
        J.record_receipt(con, attempt_id=aid, logical_leg="spot", receipt=parse_receipt(fx("tx_jup_buy.json")["result"]),
                         amounts=None, native=None, commitment="confirmed")
    for sql in ("UPDATE sol_tx_attempts SET message_hash='00'", "UPDATE sol_tx_attempts SET signed_payload=x'00'",
                "UPDATE sol_tx_attempts SET signature='x'", "UPDATE sol_tx_attempts SET plan_json='{}'",
                "DELETE FROM sol_tx_attempts", "UPDATE sol_tx_evidence SET kind='x'", "DELETE FROM sol_tx_evidence",
                "UPDATE sol_receipts SET flows_json='{\"out_raw\":\"1\"}'", "UPDATE sol_receipts SET slot=slot+1",
                "DELETE FROM sol_receipts"):
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(sql)


def test_input_validation(db):
    con, _ = db
    with pytest.raises(TypeError):
        new(con, plan={"amount_in_raw": 1.5})              # float в журнал не пишется
    with pytest.raises(J.JournalError):
        J.new_attempt(con, network=MAINNET_GENESIS, wallet=WALLET, logical_action_id="x", provider="p", path="p",
                      payload_kind="tx", message_hash="zz", recent_blockhash=SELL.message.recent_blockhash,
                      last_valid_block_height=1, lvbh_exact=True, blockhash_slot=1, plan={})
    with pytest.raises(J.JournalError):
        new(con, lvbh=None, exact=True)
    with pytest.raises(Base58Error):
        new(con, wallet=WALLET.lower())
    with pytest.raises(J.JournalError):
        new(con, predecessor_attempt_id="sa-nope")
    aid = new(con)
    with pytest.raises(J.BadTransition):
        J.apply_resolution(con, aid, UNK)                  # не подписана — исхода в сети нет
    with pytest.raises(J.BadTransition):
        J.mark_broadcast(con, aid, endpoint="a")
    with pytest.raises(J.JournalError):
        J.record_send_result(con, aid, result="maybe")
    J.abandon_unsigned(con, aid, "план истёк")
    with pytest.raises(J.BadTransition):
        J.record_signed(con, aid, SELL_BYTES)
    a2 = new(con, predecessor_attempt_id=aid, request_id="req-1", provider_pending=True)
    assert J.attempt(con, a2)["provider_pending"] == 1
    with pytest.raises(J.JournalBusy):                     # тот же requestId провайдера — второй попыткой не бывает
        new(con, action="clip-2", request_id="req-1")
    with pytest.raises(J.JournalError):
        J.attempt(con, "sa-nope")


def test_schema_idempotent_and_newer_refused(db):
    con, _ = db
    J.ensure_schema(con)
    J.ensure_schema(con)
    assert con.execute("SELECT v FROM sol_meta WHERE k='schema'").fetchone()[0] == str(J.SCHEMA_VERSION)
    con.execute("UPDATE sol_meta SET v='99' WHERE k='schema'")
    with pytest.raises(J.JournalError, match="новее"):
        J.ensure_schema(con)


# --- F1: попытка в сети без чека не теряется при восстановлении ------------------------------------
class Node:
    """Узел RPC для резолвера: статус подписи и getTransaction (который может запаздывать — None)."""

    def __init__(self, label, commitment, tx):
        self.label, self.tx = label, tx
        self.status = {"slot": SELL_JSON["slot"], "confirmationStatus": commitment, "err": None}

    def signature_statuses(self, sigs, *, search_history=True):
        return [self.status], 1

    def transaction(self, sig, *, commitment="confirmed"):
        return self.tx


def nodes(commitment, tx):
    return [Node("a", commitment, tx), Node("b", commitment, tx)]


def receipts(con):
    return con.execute("SELECT count(*) FROM sol_receipts").fetchone()[0]


def test_f1_finalized_without_receipt_found_after_restart(db):
    con, path = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    J.mark_broadcast(con, aid, endpoint="a")
    ref = J.attempt_ref(J.attempt(con, aid))
    r1 = resolve(ref, nodes("confirmed", None))            # статус есть, чека ещё нет — UNKNOWN_AMOUNT
    assert r1.outcome == Outcome.LANDED_OK and not r1.amounts_known
    assert J.apply_resolution(con, aid, r1) == "CONFIRMED_OK"
    r2 = resolve(ref, nodes("finalized", None))
    assert J.apply_resolution(con, aid, r2) == "FINALIZED_OK" and receipts(con) == 0
    con.close()                                            # упали до чека
    con2 = store.connect(path)
    J.ensure_schema(con2)
    pend = J.unresolved(con2)
    assert [(r["attempt_id"], r["state"]) for r in pend] == [(aid, "FINALIZED_OK")]
    with pytest.raises(J.JournalError, match="уже исполнено"):  # второго свопа нет, а купленное найдено
        new(con2, predecessor_attempt_id=aid)
    for row in pend:                                       # цикл восстановления: резолвер → исход → чек
        res = resolve(J.attempt_ref(row), nodes("finalized", SELL_JSON))
        assert J.apply_resolution(con2, row["attempt_id"], res) == "FINALIZED_OK"
        rc = parse_receipt(res.tx, expect_signature=row["signature"])
        assert J.record_receipt(con2, attempt_id=row["attempt_id"], logical_leg="spot", receipt=rc, amounts=None,
                                native=None, commitment=res.commitment) == "new"
    assert J.unresolved(con2) == [] and receipts(con2) == 1
    con2.close()


def test_f1_finalized_with_only_confirmed_receipt_stays_pending(db):
    con, _ = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    J.apply_resolution(con, aid, landed())
    rec = dict(attempt_id=aid, logical_leg="spot", receipt=parse_receipt(SELL_JSON), amounts=None, native=None)
    assert J.record_receipt(con, **rec, commitment="confirmed") == "new"
    assert J.apply_resolution(con, aid, landed("finalized")) == "FINALIZED_OK"
    assert [r["state"] for r in J.unresolved(con)] == ["FINALIZED_OK"]  # упали до «finality» — чек не финален
    assert J.record_receipt(con, **rec, commitment="finalized") == "finality"
    assert J.unresolved(con) == []


def test_f1_finalized_err_without_receipt_pending_for_fee(db):
    con, _ = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    assert J.apply_resolution(con, aid, landed("finalized", ok=False, tx=None)) == "FINALIZED_ERR"
    assert [r["state"] for r in J.unresolved(con)] == ["FINALIZED_ERR"]  # комиссия ещё не учтена
    rc = parse_receipt(ERR_JSON)
    assert not rc.ok
    assert J.record_receipt(con, attempt_id=aid, logical_leg="spot", receipt=rc, amounts=None, native=None,
                            commitment="finalized") == "new"
    assert J.unresolved(con) == []


# --- F5: чек принимается только у попытки в сети и с тем же исходом --------------------------------
REFUSE = [
    ("SIGNED_DURABLE", [], SELL_JSON, "confirmed", "не в сети|только у попытки в сети"),
    ("UNKNOWN", [UNK], SELL_JSON, "confirmed", "только у попытки в сети"),
    ("EXPIRED_NOT_LANDED", [EXPIRED], SELL_JSON, "confirmed", "только у попытки в сети"),
    ("ROLLED_BACK", [landed(), EXPIRED], SELL_JSON, "confirmed", "только у попытки в сети"),
    ("FINALIZED_ERR", [landed("finalized", ok=False)], SELL_JSON, "finalized", "ok=True"),
    ("CONFIRMED_ERR", [landed(ok=False)], SELL_JSON, "confirmed", "ok=True"),
    ("CONFIRMED_OK", [landed()], ERR_JSON, "confirmed", "ok=False"),
    ("CONFIRMED_OK", [landed()], SELL_JSON, "finalized", "лишь CONFIRMED_OK"),
]


@pytest.mark.parametrize("state,steps,txj,commitment,msg", REFUSE, ids=[f"{c[0]}-{c[3]}-{c[2] is ERR_JSON}"
                                                                         for c in REFUSE])
def test_f5_receipt_refused_unless_landed_and_consistent(db, state, steps, txj, commitment, msg):
    con, path = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    for r in steps:
        J.apply_resolution(con, aid, r)
    assert J.attempt(con, aid)["state"] == state
    with pytest.raises(J.JournalError, match=msg):
        J.record_receipt(con, attempt_id=aid, logical_leg="spot", receipt=parse_receipt(txj), amounts=None,
                         native=None, commitment=commitment)
    assert J.attempt(con, aid)["state"] == state and receipts(con) == 0 and not con.in_transaction
    other = store.connect(path)                            # отказ записан и закоммичен — видно другому соединению
    ev = other.execute("SELECT state_from, json FROM sol_tx_evidence WHERE attempt_id=? AND kind='receipt_refused'",
                       (aid,)).fetchall()
    other.close()
    assert len(ev) == 1 and ev[0][0] == state and '"leg":"spot"' in ev[0][1]


def test_f5_repeat_after_rollback_refused_err_receipt_on_err_accepted(db):
    con, _ = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    J.apply_resolution(con, aid, landed())
    rec = dict(attempt_id=aid, logical_leg="spot", receipt=parse_receipt(SELL_JSON), amounts=None, native=None)
    assert J.record_receipt(con, **rec, commitment="confirmed") == "new"
    assert J.apply_resolution(con, aid, EXPIRED) == "ROLLED_BACK"
    with pytest.raises(J.JournalError, match="ROLLED_BACK"):        # не «same»: исполнение откатилось
        J.record_receipt(con, **rec, commitment="confirmed")
    con.execute("CREATE TABLE eng(x)")
    with store.tx(con):                                    # внутри транзакции движка: поймал — свидетельство с ней
        con.execute("INSERT INTO eng VALUES (1)")
        with pytest.raises(J.JournalError):
            J.record_receipt(con, **rec, commitment="finalized")
    kinds = [e["kind"] for e in J.evidence(con, aid)]
    assert kinds.count("receipt_refused") == 2 and receipts(con) == 1


def test_f5_err_receipt_on_confirmed_err_accepted(db):
    con, _ = db
    aid = new(con)
    J.record_signed(con, aid, SELL_BYTES)
    assert J.apply_resolution(con, aid, landed(ok=False)) == "CONFIRMED_ERR"
    rec = dict(attempt_id=aid, logical_leg="spot", receipt=parse_receipt(ERR_JSON), amounts=None, native=None)
    assert J.record_receipt(con, **rec, commitment="confirmed") == "new"
    with pytest.raises(J.JournalError, match="лишь CONFIRMED_ERR"):
        J.record_receipt(con, **rec, commitment="finalized")
    assert J.apply_resolution(con, aid, landed("finalized", ok=False)) == "FINALIZED_ERR"
    assert J.record_receipt(con, **rec, commitment="finalized") == "finality"
