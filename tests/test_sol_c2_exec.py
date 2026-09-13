"""Шаг C2: исполнитель спота Solana (sol_exec.SolanaExecutor) на фейковой цепи — запись-до (X01), только показ
готовой транзакции внешнего подписанта (G14), подпись лишь проверенных байтов (S13), истечение по точной паре
blockhash/высота у двух RPC и лишь затем новая попытка (X04/X05), те же байты при повторе (X02), фактические суммы
с возвратом роутера (S14), упавшая в сети транзакция (S16), исход из журнала без сети."""
import sqlite3
from dataclasses import replace
import pytest
from funding_bot.trade import instruments as I, sol_exec
from funding_bot.trade.sol_exec import PresendRefused
from funding_bot.trade.sol_flow import SolDesk
from funding_bot.trade.spot_router import Payload
import sol_c2_world as W

pytest.importorskip("solders")


def setup(tmp_path):
    w = W.make_world(tmp_path)
    inst = I.deal_spec(w.registry.get("ansem_sol_para_v1"), account_id=W.ACCOUNT_ID)
    sd = SolDesk(w.desk)
    req = sd.request(w.live, inst, "entry", 150_000_000, w.loader(), "entry")
    return w, req, (lambda: W.Prov(w.sol, w.clock, "jupiter", ("jupiter_build_v2",)).candidates(req)[0])


def swap(w, req, cand, out, logical="O1:1"):
    return w.spot.swap(w.con, cand, req, logical_action_id=logical, clip_ref="1",
                       meta={"deal_id": "D1", "clip_id": 1, "op_id": None}, min_validity_heights=60, apply=out.append)


def rows(w):
    return [dict(r) for r in w.con.execute("SELECT attempt_id, state, signature, predecessor_attempt_id FROM "
                                           "sol_tx_attempts ORDER BY created")]


def test_x01_signed_but_not_journaled_is_never_sent(tmp_path, monkeypatch):
    w, req, cand = setup(tmp_path)

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(sol_exec.J, "record_signed", boom)
    with pytest.raises(PresendRefused, match="не записана"):
        swap(w, req, cand(), [])
    assert w.sol.sends == [] and [r["state"] for r in rows(w)] == ["ABANDONED_UNSIGNED"]


def test_g14_provider_ready_tx_is_show_only(tmp_path):
    w, req, cand = setup(tmp_path)
    c = cand()
    with pytest.raises(PresendRefused, match="только показ"):
        swap(w, req, replace(c, payload=Payload("tx", "base64", data=c.payload.data)), [])
    assert rows(w) == [] and w.sol.sends == []


def test_s13_unvalidated_bytes_are_not_signed(tmp_path):
    w, req, cand = setup(tmp_path)
    c = cand()
    w.validator.refuse.add(c.message_hash)
    with pytest.raises(PresendRefused, match="не проверены"):
        swap(w, req, c, [])
    assert rows(w) == [] and w.sol.sends == []


def test_x05_expiry_proven_then_successor_attempt(tmp_path):
    w, req, cand = setup(tmp_path)
    out: list = []
    w.sol.blackhole = True
    o = swap(w, req, cand(), out)
    assert o.state == "expired" and out[-1].state == "expired" and rows(w)[0]["state"] == "EXPIRED_NOT_LANDED"
    assert len(set(w.sol.sends)) == 1                          # повторы — те же байты
    w.sol.blackhole = False
    o2 = swap(w, req, cand(), out)                             # новая котировка/подпись — только после доказанного
    r = rows(w)
    assert o2.state == "ok" and len(r) == 2 and r[1]["predecessor_attempt_id"] == r[0]["attempt_id"]
    assert r[1]["signature"] != r[0]["signature"] and r[1]["state"] == "FINALIZED_OK"


def test_x04_one_rpc_silent_stays_unknown_and_blocks_new_route(tmp_path):
    w, req, cand = setup(tmp_path)
    w.sol.blackhole, w.sol.down = True, {"b"}
    o = swap(w, req, cand(), [])
    assert o.state == "unknown" and rows(w)[0]["state"] == "UNKNOWN"
    with pytest.raises(PresendRefused, match="журнал"):        # R12: новый маршрут поверх UNKNOWN не подписывается
        swap(w, req, W.Prov(w.sol, w.clock, "okx", ("okx_solana_v6",)).candidates(req)[0], [])
    assert len(rows(w)) == 1


def test_x02_lost_response_retransmits_same_bytes_one_signature(tmp_path):
    w, req, cand = setup(tmp_path)
    w.sol.script = [{"beh": "drop"}]                           # первый ответ потерян, байты не дошли; повтор садится
    o = swap(w, req, cand(), [])
    assert o.state == "ok" and len(rows(w)) == 1 and len(w.sol.sends) >= 2 and len(set(w.sol.sends)) == 1


def test_s14_refund_counts_actual_debit(tmp_path):
    w, req, cand = setup(tmp_path)
    w.sol.script = [{"beh": "land", "refund": 10_000_000}]
    o = swap(w, req, cand(), [])
    assert (o.state, o.in_raw, o.out_raw) == ("ok", 140_000_000, 903_000_000)


def test_s16_landed_with_error_is_failed_and_fee_is_charged(tmp_path):
    w, req, cand = setup(tmp_path)
    w.sol.script = [{"beh": "err"}]
    out: list = []
    o = swap(w, req, cand(), out)
    assert (o.state, o.in_raw, o.out_raw) == ("failed", 0, 0) and out[-1] is o
    assert [(f.kind, f.amount_raw) for f in o.fees] == [("network_total", 5020)]


def test_recorded_receipt_applies_without_network(tmp_path):
    w, req, cand = setup(tmp_path)
    o = swap(w, req, cand(), [])
    w.sol.down = {"a", "b"}
    out: list = []
    again = w.spot.resolve(w.con, o.attempt_id, apply=out.append)
    assert (again.state, again.in_raw, again.out_raw, again.receipt) == ("ok", o.in_raw, o.out_raw, "same")
    assert out == [again]
