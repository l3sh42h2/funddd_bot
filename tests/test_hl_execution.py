"""Поток hl: исполнение — сквозной /exchange против эталона SDK, запись-до и одна отправка (X01), исходы IOC
(H10/H12), разбор UNKNOWN через expiresAfter (H11), сохранённый cloid через рестарт (H13), reduceOnly (H14),
плечо/isolated (H09), durable nonce, ворота режима. Ответы /exchange — синтетика по документации (своих ордеров нет)."""
import sqlite3, threading
from decimal import Decimal as D
import pytest
import requests
from funding_bot.client import BudgetExceeded
from funding_bot.trade import hl_rules as R
from funding_bot.trade import hyperliquid_trade as ht
from funding_bot.trade.hyperliquid_trade import (HlApiError, HlError, HlJournal, HlSigner, HyperliquidTrade,
                                                 connect_journal)
from funding_bot.trade.keys import KeyMismatch, ModeForbidden
import hl_support as S

eth_account = pytest.importorskip("eth_account")
from eth_account import Account                                   # noqa: E402

A = "para:ANSEM"
T0MS = int(S.T0 * 1000)


def make(tmp_path, account=S.MASTER, master=S.MASTER, *, journal_cls=HlJournal, mode="live", paused=False, **kw):
    clock = S.Clock()
    fake = S.FakeHL(clock)
    jr = journal_cls(connect_journal(tmp_path / "hl.db"), now=clock)
    sg = HlSigner(Account.from_key(S.TEST_KEY), agent=S.AGENT, master=master, account=account)
    st = {"mode": mode, "paused": paused}
    t = HyperliquidTrade(account, master=master, signer=sg, journal=jr, session=fake, now=clock, sleep=clock.sleep,
                         mode_state=lambda: (st["mode"], st["paused"]), **kw)
    return t, fake, clock, st


def last_nonce(t):
    r = t.journal.con.execute("SELECT MAX(last_nonce) FROM hl_nonces").fetchone()[0]
    return r


# --- сквозной путь ----------------------------------------------------------------------------------
@pytest.mark.parametrize("case", S.E2E["cases"], ids=lambda c: c["name"])
def test_ioc_payload_byte_equal_to_sdk_golden(tmp_path, case):
    t, fake, clock, _ = make(tmp_path, account=case["account"], master=case["master"])
    if case["reduce_only"]:
        fake.user(case["account"], "clearinghouseState", S.ch([(A, "-1500.0")]))
    fake.exchange_script.append(S.Resp(200, S.ok_filled(case["sz"], case["px"], cloid=case["cloid"])))
    seen = []
    f = t.ioc(A, case["side"], D(case["sz"]), D(case["px"]), case["client_id"], case["reduce_only"],
              on_signed=lambda n: seen.append((n, len(fake.exchange_calls()))))
    p = case["payload"]
    assert fake.exchange_calls() == [p]                          # действие, nonce, подпись, vault, expiresAfter
    assert seen == [(p["nonce"], 0)]                             # on_signed — до отправки
    assert (f.status, f.qty, f.avg_px, f.cloid, f.sign_nonce, f.err_code, f.outcome) == (
        "FILLED", D(case["sz"]), D(case["px"]), case["cloid"], p["nonce"], None, "FILLED_TERMINAL")
    assert f.quote == D(case["sz"]) * D(case["px"]) and f.expires_after == p["expiresAfter"]
    row = t.journal.get(case["client_id"])
    assert (row["cloid"], row["nonce"], row["expires_after"], row["vault"], row["state"]) == (
        case["cloid"], p["nonce"], p["expiresAfter"], p["vaultAddress"], "FILLED")
    assert row["signer"] == S.AGENT.lower() and row["account"] == case["account"].lower()


# --- исходы IOC -------------------------------------------------------------------------------------
def test_h10_partial_terminal_then_query_counts_once(tmp_path):
    t, fake, clock, _ = make(tmp_path)
    fake.exchange_script.append(S.Resp(200, S.ok_filled("60.0", "0.1661", oid=91)))
    f = t.ioc(A, "SELL", D(100), D("0.166"), "c-part", False)
    assert (f.status, f.qty, f.order_id, f.outcome) == ("PARTIALLY_FILLED", D(60), 91, "PARTIAL_TERMINAL")
    assert t.journal.get("c-part")["filled"] == "60.0" and t.pending() == []
    fake.user(S.MASTER, "orderStatus", lambda b: S.order_status(f.cloid, 91, "100.0", "40.0", "canceled"))
    fake.user(S.MASTER, "userFillsByTime", [S.fill(91, "30.0", "0.1661", T0MS + 50, 1),
                                            S.fill(91, "30.0", "0.1661", T0MS + 50, 2)])
    q = t.query(A, "c-part")
    assert (q.status, q.qty, q.avg_px, q.order_id) == ("PARTIALLY_FILLED", D(60), D("0.1661"), 91)


def test_ioc_no_match_is_expired_zero_fill(tmp_path):
    t, fake, _, _ = make(tmp_path)
    fake.exchange_script.append(S.Resp(200, S.ok_error(S.IOC_NO_MATCH)))
    f = t.ioc(A, "SELL", D(100), D("0.166"), "c-exp", False)
    assert (f.status, f.qty, f.err_kind, f.outcome, f.err_code) == ("EXPIRED", 0, "ioc_no_match",
                                                                     "REJECTED_ZERO_FILL", None)
    assert t.journal.get("c-exp")["state"] == "EXPIRED" and t.pending() == []


@pytest.mark.parametrize("resp,status,kind", [
    (S.Resp(200, S.ok_error("Order must have minimum value of $10.")), "REJECTED", "min_notional"),
    (S.Resp(200, S.ok_error("Reduce only order would increase position.")), "REJECTED", "reduce_only"),
    (S.Resp(200, S.err("User or API Wallet 0x1234 does not exist.")), "REJECTED", "signature_or_agent"),
    (requests.exceptions.ReadTimeout("read timed out"), "UNKNOWN", None),
    (requests.exceptions.ConnectionError("reset"), "UNKNOWN", None),
    (S.Resp(502, raw=b"<html>bad gateway</html>"), "UNKNOWN", None),
    (S.Resp(200, raw=b"not json"), "UNKNOWN", None),
    (S.Resp(429, raw=b"rate limited"), "UNKNOWN", None),
    (S.Resp(200, S.ok_resting(5)), "UNKNOWN", None),
], ids=["min_ntl", "reduce_only", "agent", "timeout", "reset", "502", "garbage", "429", "resting"])
def test_h12_per_order_error_vs_transport_uncertainty(tmp_path, resp, status, kind):
    t, fake, _, _ = make(tmp_path)
    fake.exchange_script.append(resp)
    f = t.ioc(A, "SELL", D(100), D("0.166"), "c-h12", False)
    assert (f.status, f.err_kind, f.qty) == (status, kind, 0)
    assert len(fake.exchange_calls()) == 1                        # одна попытка, без повтора
    assert t.journal.get("c-h12")["state"] == status
    if status == "REJECTED":
        assert t.last_error and t.pending() == []
    else:
        assert [r["client_id"] for r in t.pending()] == ["c-h12"]


def test_x01_journal_failure_before_send_means_no_send(tmp_path):
    class BadJournal(HlJournal):
        def signed(self, client_id, sig):
            raise sqlite3.OperationalError("disk I/O error")

    t, fake, _, _ = make(tmp_path / "a", journal_cls=BadJournal)
    with pytest.raises(sqlite3.OperationalError):
        t.ioc(A, "SELL", D(100), D("0.166"), "c-x01", False)
    assert fake.exchange_calls() == [] and t.journal.get("c-x01")["state"] == "NOT_SENT" and t.pending() == []

    t, fake, _, _ = make(tmp_path / "b")

    def boom(n):
        raise RuntimeError("perp_orders не записан")
    with pytest.raises(RuntimeError):
        t.ioc(A, "SELL", D(100), D("0.166"), "c-x01b", False, on_signed=boom)
    assert fake.exchange_calls() == [] and t.journal.get("c-x01b")["state"] == "NOT_SENT"
    with pytest.raises(HlError, match="уже отправлялся"):            # тот же client_id — никогда повторно
        t.ioc(A, "SELL", D(100), D("0.166"), "c-x01b", False)
    fake.exchange_script.append(S.Resp(200, S.ok_filled("100", "0.1661")))
    assert t.ioc(A, "SELL", D(100), D("0.166"), "c-x01c", False).status == "FILLED"


def test_x10_unknown_blocks_new_orders_also_after_restart(tmp_path):
    t, fake, clock, _ = make(tmp_path)
    fake.exchange_script.append(requests.exceptions.ReadTimeout("t"))
    assert t.ioc(A, "SELL", D(100), D("0.166"), "c-u1", False).status == "UNKNOWN"
    with pytest.raises(HlError, match="неизвестным исходом"):
        t.ioc(A, "SELL", D(100), D("0.166"), "c-u2", False)
    assert len(fake.exchange_calls()) == 1
    fake2 = S.FakeHL(clock)
    t2 = HyperliquidTrade(S.MASTER, signer=t.signer, journal=HlJournal(connect_journal(tmp_path / "hl.db"), now=clock),
                          session=fake2, now=clock, sleep=clock.sleep, mode_state=lambda: ("live", False))
    assert [r["client_id"] for r in t2.pending()] == ["c-u1"]
    with pytest.raises(HlError, match="неизвестным исходом"):
        t2.ioc(A, "SELL", D(100), D("0.166"), "c-u3", False)
    assert fake2.exchange_calls() == []


def test_params_refused_before_nonce_and_network(tmp_path):
    t, fake, _, _ = make(tmp_path)
    for args in ((A, "SELL", D("1000.1"), D("0.166")), (A, "SELL", D(100), D("0.158381")),
                 (A, "SELL", D(50), D("0.1")), (A, "SELL", 100.0, D("0.166")), ("ANSEM", "SELL", D(100), D("0.166")),
                 (A, "sell", D(100), D("0.166")), (A, "SELL", D(0), D("0.166"))):
        with pytest.raises(ValueError):
            t.ioc(*args, "c-bad", False)
    assert fake.exchange_calls() == [] and last_nonce(t) is None and t.journal.get("c-bad") is None


def test_h14_reduce_only_never_flips_or_drops_flag(tmp_path):
    t, fake, _, _ = make(tmp_path)
    for pos in (S.ch([(A, "-1.0")]), S.ch([]), S.ch([(A, "5.0")]), S.Resp(500, raw=b"x")):
        fake.user(S.MASTER, "clearinghouseState", pos)
        with pytest.raises(HlError, match="H14"):
            t.ioc(A, "BUY", D(2), D("10"), "c-ro", True)
    fake.user(S.MASTER, "clearinghouseState", S.ch([(A, "-5.0")]))
    with pytest.raises(HlError, match="H14"):
        t.ioc(A, "SELL", D(2), D("0.166"), "c-ro", True)
    assert fake.exchange_calls() == []
    fake.exchange_script.append(S.Resp(200, S.ok_filled("2", "0.167")))
    f = t.ioc(A, "BUY", D(2), D("0.17"), "c-ro-ok", True)                # < $10 reduceOnly — не блокируем сами
    assert f.status == "FILLED"
    o = fake.exchange_calls()[0]["action"]["orders"][0]
    assert (o["b"], o["r"], o["s"], o["p"]) == (True, True, "2", "0.17")


# --- разбор UNKNOWN -----------------------------------------------------------------------------------
def _timeout_order(tmp_path, cid="c-u", qty=D(100)):
    t, fake, clock, st = make(tmp_path)
    fake.exchange_script.append(requests.exceptions.ReadTimeout("t"))
    assert t.ioc(A, "SELL", qty, D("0.166"), cid, False).status == "UNKNOWN"
    return t, fake, clock, t.journal.get(cid)


def test_h11_timeout_then_found_filled_once_by_saved_cloid(tmp_path):
    t, fake, clock, row = _timeout_order(tmp_path, "c-h11")
    asked = []

    def os_(b):
        asked.append(b["oid"])
        return S.order_status(row["cloid"], 555, "100.0", "0.0", "filled")
    fake.user(S.MASTER, "orderStatus", os_)
    fake.user(S.MASTER, "userFillsByTime", [S.fill(555, "40.0", "0.1661", T0MS + 50, 1),
                                            S.fill(555, "60.0", "0.1662", T0MS + 50, 2)])
    n0 = last_nonce(t)
    s = t.settle_unknown(A, "c-h11", pos_before=D(0), since_ms=T0MS)
    assert (s.status, s.qty, s.order_id, s.sign_nonce) == ("FILLED", D(100), 555, row["nonce"])
    assert s.avg_px == (D(40) * D("0.1661") + D(60) * D("0.1662")) / D(100)
    assert asked == [row["cloid"]] and last_nonce(t) == n0 and len(fake.exchange_calls()) == 1
    assert t.journal.get("c-h11")["state"] == "FILLED" and t.pending() == []
    before = len(fake.calls)
    s2 = t.settle_unknown(A, "c-h11", pos_before=D(0), since_ms=T0MS)      # повтор — из журнала, без чтений
    assert (s2.status, s2.qty) == ("FILLED", D(100)) and len(fake.calls) == before


def test_h13_saved_mapping_survives_restart_and_tamper_is_refused(tmp_path):
    cid = "fb-D7K2-e03-c1-a1"                          # формат движка — не wire cloid
    t, fake, clock, row = _timeout_order(tmp_path, cid)
    sent = fake.exchange_calls()[0]["action"]["orders"][0]["c"]
    assert sent == row["cloid"] == R.derive_cloid("mainnet", S.MASTER, cid) and sent != cid
    fake2 = S.FakeHL(clock)
    asked = []
    fake2.user(S.MASTER, "orderStatus", lambda b: asked.append(b["oid"]) or S.UNKNOWN_OID)
    t2 = HyperliquidTrade(S.MASTER, signer=t.signer, journal=HlJournal(connect_journal(tmp_path / "hl.db"), now=clock),
                          session=fake2, now=clock, sleep=clock.sleep, mode_state=lambda: ("live", False))
    q = t2.query(A, cid)
    assert q.status == "NOT_FOUND" and asked == [row["cloid"]] and q.sign_nonce == row["nonce"]
    t2.journal.con.execute("UPDATE hl_order_attempts SET cloid=? WHERE client_id=?", ("0x" + "f" * 32, cid))
    q = t2.query(A, cid)
    assert q.status == "UNKNOWN" and "другого" in q.err_text and asked == [row["cloid"]]


def test_settle_not_found_only_after_expiry_with_full_evidence(tmp_path):
    t, fake, clock, row = _timeout_order(tmp_path, "c-nf")
    polls = []
    fake.user(S.MASTER, "orderStatus", lambda b: polls.append(clock()) or S.UNKNOWN_OID)
    fake.user(S.MASTER, "clearinghouseState", S.ch([]))
    fake.user(S.MASTER, "userFillsByTime", [])
    s0 = t.settle_unknown(A, "c-nf", pos_before=D(0), since_ms=T0MS, wait=False)
    assert s0.status == "UNKNOWN"                                         # unknownOid до срока — не доказательство
    s = t.settle_unknown(A, "c-nf", pos_before=D(0), since_ms=T0MS)
    assert s.status == "NOT_FOUND" and s.outcome == "NOT_SUBMITTED_PROVEN"
    after = [x for x in polls if x * 1000 > row["expires_after"] + ht.CLOCK_SLACK_MS]
    assert len(after) == ht.NOT_FOUND_POLLS and clock() * 1000 > row["expires_after"] + ht.CLOCK_SLACK_MS
    assert t.journal.get("c-nf")["state"] == "NOT_FOUND" and t.pending() == []
    fake.exchange_script.append(S.Resp(200, S.ok_filled("100", "0.1661")))
    assert t.ioc(A, "SELL", D(100), D("0.166"), "c-nf-a2", False).status == "FILLED"
    assert len(fake.exchange_calls()) == 2


@pytest.mark.parametrize("variant", ["pos_moved", "pos_unknown", "foreign_fill", "own_fill_no_status", "fills_gap",
                                     "no_pos_before"])
def test_settle_stays_unknown_without_full_evidence(tmp_path, variant):
    t, fake, clock, row = _timeout_order(tmp_path, "c-uk")
    fake.user(S.MASTER, "orderStatus", S.UNKNOWN_OID)
    fake.user(S.MASTER, "clearinghouseState", S.ch([(A, "-100.0")] if variant == "pos_moved" else []))
    if variant == "pos_unknown":
        fake.user(S.MASTER, "clearinghouseState", S.Resp(500, raw=b"x"))
    fills = []
    if variant == "foreign_fill":
        fills = [S.fill(999, "5.0", "0.166", T0MS + 10, 7)]
    if variant == "own_fill_no_status":
        fills = [S.fill(1000, "5.0", "0.166", T0MS + 10, 8, cloid=row["cloid"])]
    if variant == "fills_gap":
        fills = [S.fill(900 + i, "1.0", "0.166", T0MS + 10, 10_000 + i) for i in range(ht.FILLS_PAGE)]
    fake.user(S.MASTER, "userFillsByTime", fills)
    s = t.settle_unknown(A, "c-uk", pos_before=None if variant == "no_pos_before" else D(0), since_ms=T0MS,
                         known_order_ids=frozenset({1000}) if variant == "own_fill_no_status" else frozenset())
    assert s.status == "UNKNOWN"
    assert t.journal.get("c-uk")["state"] == "UNKNOWN" and len(t.pending()) == 1
    assert len(fake.exchange_calls()) == 1


def test_settle_waits_for_fills_to_catch_up(tmp_path):
    t, fake, clock, row = _timeout_order(tmp_path, "c-lag")
    fake.user(S.MASTER, "orderStatus", S.order_status(row["cloid"], 77, "100.0", "40.0", "canceled"))
    calls = {"n": 0}

    def fills(b):
        calls["n"] += 1
        return [] if calls["n"] < 3 else [S.fill(77, "60.0", "0.1663", T0MS + 20, 3)]
    fake.user(S.MASTER, "userFillsByTime", fills)
    s = t.settle_unknown(A, "c-lag", pos_before=D(0), since_ms=T0MS)
    assert (s.status, s.qty, s.avg_px) == ("PARTIALLY_FILLED", D(60), D("0.1663"))
    assert calls["n"] == 3 and clock() > S.T0


def test_settle_found_zero_fill_statuses(tmp_path):
    t, fake, clock, row = _timeout_order(tmp_path, "c-z")
    fake.user(S.MASTER, "orderStatus", S.order_status(row["cloid"], 78, "100.0", "100.0", "iocCancelRejected"))
    s = t.settle_unknown(A, "c-z", pos_before=D(0), since_ms=T0MS)
    assert (s.status, s.qty, s.err_kind) == ("EXPIRED", 0, "ioc_no_match")
    t, fake, clock, row = _timeout_order(tmp_path / "b", "c-z2")
    fake.user(S.MASTER, "orderStatus", S.order_status(row["cloid"], 79, "100.0", "100.0", "perpMarginRejected"))
    s = t.settle_unknown(A, "c-z2", pos_before=D(0), since_ms=T0MS)
    assert (s.status, s.err_kind) == ("REJECTED", "margin")
    t, fake, clock, row = _timeout_order(tmp_path / "c", "c-z3")          # чужая заявка под нашим cloid — аномалия
    fake.user(S.MASTER, "orderStatus", S.order_status(row["cloid"], 80, "55.0", "0.0", "filled"))
    s = t.settle_unknown(A, "c-z3", pos_before=D(0), since_ms=T0MS, wait=False)
    assert s.status == "UNKNOWN" and s.anomaly


# --- HL-2: найденную заявку unknownOid не отменяет; unknownOid считаются только подряд ----------------
def _post_expiry(row, clock, before, script):
    """orderStatus: до срока — before, после — по script (последний элемент повторяется)."""
    post = []

    def os_(b):
        if clock() * 1000 <= row["expires_after"] + ht.CLOCK_SLACK_MS:
            return before
        post.append(script[min(len(post), len(script) - 1)])
        return post[-1]
    return os_, post


def _lagging_node(fake):
    fake.user(S.MASTER, "clearinghouseState", S.ch([]))     # отстающий узел: позиция как до заявки
    fake.user(S.MASTER, "userFillsByTime", [])             # и fills пусты


def test_hl2_found_before_expiry_then_unknownoid_stays_unknown(tmp_path):
    t, fake, clock, row = _timeout_order(tmp_path, "c-hl2a")
    found = S.order_status(row["cloid"], 91, "100.0", "40.0", "canceled")    # исполнено 60, fills отстают
    os_, post = _post_expiry(row, clock, found, [S.UNKNOWN_OID])
    fake.user(S.MASTER, "orderStatus", os_)
    _lagging_node(fake)
    s = t.settle_unknown(A, "c-hl2a", pos_before=D(0), since_ms=T0MS)
    assert len(post) >= ht.NOT_FOUND_POLLS                                 # unknownOid после срока были
    assert s.status == "UNKNOWN" and s.outcome == "UNKNOWN" and s.order_id == 91
    got = t.journal.get("c-hl2a")
    assert got["state"] == "UNKNOWN" and got["oid"] == 91 and len(t.pending()) == 1
    fake.exchange_script.append(S.Resp(200, S.ok_filled("100", "0.166")))
    with pytest.raises(HlError, match="неизвестным исходом"):              # лишнего шорта на исполненные 60 нет
        t.ioc(A, "SELL", D(100), D("0.166"), "c-hl2a-2", False)
    assert len(fake.exchange_calls()) == 1


def test_hl2_unknownoid_not_consecutive_around_found_open(tmp_path):
    t, fake, clock, row = _timeout_order(tmp_path, "c-hl2b")
    op = S.order_status(row["cloid"], 92, "100.0", "100.0", "open")
    os_, post = _post_expiry(row, clock, S.UNKNOWN_OID, [S.UNKNOWN_OID, op, S.UNKNOWN_OID])
    fake.user(S.MASTER, "orderStatus", os_)
    _lagging_node(fake)
    s = t.settle_unknown(A, "c-hl2b", pos_before=D(0), since_ms=T0MS)
    assert post[:3] == [S.UNKNOWN_OID, op, S.UNKNOWN_OID] and len(post) > 3
    assert s.status == "UNKNOWN" and s.order_id == 92 and s.anomaly           # открытая — аномалия, не NOT_FOUND
    assert t.journal.get("c-hl2b")["state"] == "UNKNOWN" and len(t.pending()) == 1


def test_hl2_found_in_earlier_settle_is_remembered(tmp_path):
    t, fake, clock, row = _timeout_order(tmp_path, "c-hl2c")
    fake.user(S.MASTER, "orderStatus", S.order_status(row["cloid"], 93, "100.0", "100.0", "open"))
    assert t.settle_unknown(A, "c-hl2c", pos_before=D(0), since_ms=T0MS, wait=False).status == "UNKNOWN"
    assert t.journal.get("c-hl2c")["oid"] == 93
    fake.user(S.MASTER, "orderStatus", S.UNKNOWN_OID)                      # потом — только unknownOid
    _lagging_node(fake)
    s = t.settle_unknown(A, "c-hl2c", pos_before=D(0), since_ms=T0MS)
    assert s.status == "UNKNOWN" and s.order_id == 93 and "уже найдена" in s.err_text
    assert clock() * 1000 > row["expires_after"] + ht.CLOCK_SLACK_MS and len(t.pending()) == 1


@pytest.mark.parametrize("script,status", [
    (["unk", "fail"], "UNKNOWN"),                   # unk, сбой, unk, сбой… — двух подряд нет
    (["fail", "unk", "unk"], "NOT_FOUND"),          # после сбоя два подряд — доказательство полное
])
def test_hl2_read_failure_breaks_unknownoid_series(tmp_path, script, status):
    t, fake, clock, row = _timeout_order(tmp_path, "c-hl2d")
    post, left = [], {"n": 0}

    def os_(b):
        if clock() * 1000 <= row["expires_after"] + ht.CLOCK_SLACK_MS:
            return S.UNKNOWN_OID
        if left["n"]:                               # повтор чтения внутри того же опроса — тоже сбой
            left["n"] -= 1
            return S.Resp(500, raw=b"x")
        i = len(post)
        post.append(script[i % len(script)] if status == "UNKNOWN" else script[min(i, len(script) - 1)])
        if post[-1] == "fail":
            left["n"] = ht.READ_RETRIES - 1         # сбой опроса = все попытки info() неудачны
            return S.Resp(500, raw=b"x")
        return S.UNKNOWN_OID
    fake.user(S.MASTER, "orderStatus", os_)
    _lagging_node(fake)
    s = t.settle_unknown(A, "c-hl2d", pos_before=D(0), since_ms=T0MS)
    assert s.status == status and t.journal.get("c-hl2d")["state"] == status
    if status == "NOT_FOUND":
        assert post == ["fail", "unk", "unk"]
    else:
        assert len(post) >= 3 and len(t.pending()) == 1


# --- плечо и isolated ----------------------------------------------------------------------------------
def test_h09_setup_isolated_leverage_journaled_and_verified(tmp_path):
    t, fake, clock, _ = make(tmp_path, account=S.SUB, master=S.MASTER)
    with pytest.raises(ValueError, match="ISOLATED"):
        t.setup(A, 1, "CROSSED")
    with pytest.raises(ValueError, match="максимума"):
        t.setup(A, 4, "ISOLATED")
    assert fake.exchange_calls() == []
    lev = {"v": 3}
    fake.user(S.SUB, "activeAssetData", lambda b: {"user": S.SUB, "coin": A, "leverage": {"type": "isolated",
              "value": lev["v"], "rawUsd": "0.0"}, "maxTradeSzs": ["0.0", "0.0"], "availableToTrade": ["0.0", "0.0"],
              "markPx": "0.1663"})
    fake.user(S.SUB, "clearinghouseState", S.ch([]))

    def ok(p):
        lev["v"] = p["action"]["leverage"]
        return S.Resp(200, S.OK_DEFAULT)
    fake.exchange_script.append(ok)
    t.setup(A, 1, "ISOLATED")
    assert fake.exchange_calls() == [S.E2E["update_leverage_sub"]["payload"]]      # байт в байт как SDK
    t.setup(A, 1, "ISOLATED")                                                     # уже так — ничего не шлём
    assert len(fake.exchange_calls()) == 1
    fake.exchange_script.append(S.Resp(200, S.OK_DEFAULT))                        # ok, но состояние прежнее
    with pytest.raises(HlError, match="успешной не считаю"):
        t.setup(A, 2, "ISOLATED")
    fake.exchange_script.append(S.Resp(200, S.err("Invalid leverage value")))
    with pytest.raises(HlApiError):
        t.setup(A, 2, "ISOLATED")
    fake.exchange_script.append(requests.exceptions.ReadTimeout("t"))
    with pytest.raises(ht.HlNetError):
        t.setup(A, 2, "ISOLATED")
    fake.user(S.SUB, "clearinghouseState", S.ch([(A, "-100.0")]))
    with pytest.raises(HlError, match="открыта позиция"):
        t.setup(A, 3, "ISOLATED")
    states = [r[0] for r in t.journal.con.execute("SELECT state FROM hl_order_attempts WHERE kind='updateLeverage' "
                                                   "ORDER BY nonce")]
    assert states == ["OK", "OK", "REJECTED", "UNKNOWN"]
    assert t.pending() == []                                                      # плечо не блокирует заявки


# --- nonce ---------------------------------------------------------------------------------------------
def test_nonce_monotonic_clock_back_restart_and_ahead_guard(tmp_path):
    p = tmp_path / "n.db"
    j = HlJournal(connect_journal(p))
    assert j.allocate_nonce("mainnet", S.AGENT, 1000) == 1000
    assert j.allocate_nonce("mainnet", S.AGENT, 1000) == 1001
    assert j.allocate_nonce("mainnet", S.AGENT, 900) == 1002                  # часы назад — без повтора
    assert j.allocate_nonce("testnet", S.AGENT, 5) == 5                        # отдельный счёт по сети
    j2 = HlJournal(connect_journal(p))                                         # «рестарт»
    assert j2.allocate_nonce("mainnet", S.AGENT.lower(), 500) == 1003
    j2.con.execute("UPDATE hl_nonces SET last_nonce=? WHERE network='mainnet'", (1003 + 3 * 86_400_000,))
    with pytest.raises(HlError, match="сутки"):
        j2.allocate_nonce("mainnet", S.AGENT, 2000)


def test_nonce_unique_across_connections_begin_immediate(tmp_path):
    p = tmp_path / "c.db"
    HlJournal(connect_journal(p))
    got: list[int] = []
    lk = threading.Lock()

    def worker():
        j = HlJournal(connect_journal(p))
        mine = [j.allocate_nonce("mainnet", S.AGENT, 7) for _ in range(100)]
        assert mine == sorted(mine)
        with lk:
            got.extend(mine)
    ts = [threading.Thread(target=worker) for _ in range(2)]
    for x in ts:
        x.start()
    for x in ts:
        x.join()
    assert sorted(got) == list(range(7, 207))


def test_journal_refuses_reused_nonce_or_client_id(tmp_path):
    j = HlJournal(connect_journal(tmp_path / "j.db"))
    base = dict(kind="order", network="mainnet", master=S.MASTER, account=S.MASTER, signer=S.AGENT.lower(),
                action_json="{}", action_hash="0x00", nonce=5)
    j.prepare(client_id="a", cloid="0x" + "1" * 32, **base)
    with pytest.raises(HlError):
        j.prepare(client_id="b", cloid="0x" + "2" * 32, **base)                # тот же nonce
    with pytest.raises(HlError):
        j.prepare(client_id="a", cloid="0x" + "3" * 32, **{**base, "nonce": 6})  # тот же client_id


# --- ворота, бюджет, ключ, noop ------------------------------------------------------------------------
def test_gate_pause_allows_only_hedge(tmp_path):
    t, fake, _, st = make(tmp_path, paused=True)
    with pytest.raises(ModeForbidden, match="пауза"):
        t.ioc(A, "SELL", D(100), D("0.166"), "c-g1", False)
    fake.exchange_script.append(S.Resp(200, S.ok_filled("100", "0.1661")))
    assert t.ioc(A, "SELL", D(100), D("0.166"), "c-g2", False, hedge=True).status == "FILLED"
    st.update(mode="readonly", paused=False)
    with pytest.raises(ModeForbidden):
        t.ioc(A, "SELL", D(100), D("0.166"), "c-g3", False, hedge=True)
    assert len(fake.exchange_calls()) == 1


def test_exchange_429_sets_backoff_before_next_signature(tmp_path):
    t, fake, clock, _ = make(tmp_path)
    fake.exchange_script.append(S.Resp(429, raw=b"slow", headers={"Retry-After": "5"}))
    assert t.ioc(A, "SELL", D(100), D("0.166"), "c-429", False).status == "UNKNOWN"
    n0 = last_nonce(t)
    with pytest.raises(BudgetExceeded):
        t.noop()
    assert last_nonce(t) == n0 and len(fake.exchange_calls()) == 1


def test_noop_signed_and_journaled(tmp_path):
    t, fake, _, _ = make(tmp_path)
    fake.exchange_script.append(S.Resp(200, S.OK_DEFAULT))
    assert t.noop() == ("ok", None)
    p = fake.exchange_calls()[0]
    assert p["action"] == {"type": "noop"} and p["vaultAddress"] is None and p["expiresAfter"] == p["nonce"] + 30_000
    h = R.action_hash(p["action"], None, p["nonce"], p["expiresAfter"])
    rec = Account.recover_message(__import__("eth_account.messages", fromlist=["x"]).encode_typed_data(
        full_message=R.l1_typed_data(h, True)), vrs=(p["signature"]["v"], int(p["signature"]["r"], 16),
                                                     int(p["signature"]["s"], 16)))
    assert rec == S.AGENT


def test_signer_key_checks(tmp_path):
    acct = Account.from_key(S.TEST_KEY)
    with pytest.raises(KeyMismatch):
        HlSigner(acct, agent=S.MASTER, master=S.MASTER)
    with pytest.raises(KeyMismatch, match="мастер"):
        HlSigner(acct, agent=S.AGENT, master=S.AGENT)
    sg = HlSigner(acct, agent=S.AGENT, master=S.MASTER, account=S.SUB)
    assert sg.vault == S.SUB and "key" not in repr(sg).lower()
    with pytest.raises(KeyMismatch):
        HyperliquidTrade(S.MASTER, signer=sg, session=S.FakeHL())
    with pytest.raises(ValueError):
        HyperliquidTrade(S.MASTER, network="devnet", session=S.FakeHL())
    with pytest.raises(ValueError):
        HyperliquidTrade(S.MASTER, expires_ms=10 ** 9, session=S.FakeHL())


def test_fills_cursor_api_is_explicitly_unsupported(tmp_path):
    t, _, _, _ = make(tmp_path)
    with pytest.raises(NotImplementedError, match="fills_since"):
        t.fills(A, None)


def test_x07_crash_after_fill_before_result_is_settled_once(tmp_path):
    """Ответ биржи получен, но итог в журнал не записан (падение): строка остаётся SIGNED → после рестарта
    сначала разбор по cloid, исполнение учитывается один раз, новой заявки нет."""
    class CrashJournal(HlJournal):
        def result(self, client_id, state, **kw):
            if state == "FILLED" and not getattr(self, "_crashed", False):
                self._crashed = True
                raise sqlite3.OperationalError("процесс упал")
            return super().result(client_id, state, **kw)

    t, fake, clock, _ = make(tmp_path, journal_cls=CrashJournal)
    fake.exchange_script.append(S.Resp(200, S.ok_filled("100", "0.1661", oid=321)))
    assert t.ioc(A, "SELL", D(100), D("0.166"), "c-x07", False).status == "FILLED"
    assert t.journal.get("c-x07")["state"] == "SIGNED"
    fake2 = S.FakeHL(clock)
    t2 = HyperliquidTrade(S.MASTER, signer=t.signer, journal=HlJournal(connect_journal(tmp_path / "hl.db"), now=clock),
                          session=fake2, now=clock, sleep=clock.sleep, mode_state=lambda: ("live", False))
    assert [r["client_id"] for r in t2.pending()] == ["c-x07"]
    with pytest.raises(HlError, match="неизвестным исходом"):
        t2.ioc(A, "SELL", D(100), D("0.166"), "c-x07-next", False)
    cloid = t2.journal.get("c-x07")["cloid"]
    fake2.user(S.MASTER, "orderStatus", S.order_status(cloid, 321, "100.0", "0.0", "filled"))
    fake2.user(S.MASTER, "userFillsByTime", [S.fill(321, "100.0", "0.1661", T0MS + 30, 9)])
    s = t2.settle_unknown(A, "c-x07", pos_before=D(0), since_ms=T0MS)
    assert (s.status, s.qty, s.order_id) == ("FILLED", D(100), 321)
    assert t2.pending() == [] and fake2.exchange_calls() == []


def test_perp_leg_protocol_shape(tmp_path):
    from funding_bot.trade.types import PerpFill, PerpLeg
    t, fake, _, _ = make(tmp_path)
    assert isinstance(t, PerpLeg) and t.venue == "hyperliquid"
    fake.exchange_script.append(S.Resp(200, S.ok_error(S.IOC_NO_MATCH)))
    f = t.ioc(A, "SELL", D(100), D("0.166"), "c-proto", False)
    assert isinstance(f, PerpFill) and f.err_code is None and f.err_kind == "ioc_no_match"
