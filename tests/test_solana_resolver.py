"""Резолвер исхода Solana: X03–X05, G08 и сценарии SOLANA_ROUTERS §8 п.8 (ответ потерян, getTransaction
запаздывает, blockhash истёк, но чек найден, RPC расходятся). Фейковые узлы — и один прогон через настоящий
транспорт на записанных ответах mainnet."""
import pytest
from funding_bot.trade.solana.receipt import parse_receipt
from funding_bot.trade.solana.resolver import AttemptRef, Outcome, resolve
from funding_bot.trade.solana.rpc import RpcPool, RpcUnavailable, SolanaRpc
from solana_helpers import FakeHttp, fx, fx_doc

SELL = fx("tx_jup_sell.json")["result"]
SIG = SELL["transaction"]["signatures"][0]
BH = SELL["transaction"]["message"]["recentBlockhash"]
ATT = AttemptRef(SIG, BH, 500, True, 100)
CONF = {"slot": 120, "confirmationStatus": "confirmed", "err": None, "confirmations": 3}
FIN = dict(CONF, confirmationStatus="finalized", confirmations=None)
TXOK = {"slot": 120, "meta": {"err": None}}
ERR = {"InstructionError": [3, {"Custom": 6001}]}


class Ep:
    def __init__(self, label, *, statuses=(None,), tx=None, height=1000, ledger=0, valid=False, fail=()):
        self.label = label
        self._st = list(statuses)
        self.tx, self.height, self.ledger, self.valid, self.fail = tx, height, ledger, valid, set(fail)
        self.calls = []

    def _chk(self, what):
        self.calls.append(what)
        if what in self.fail:
            raise RpcUnavailable(f"{self.label}: {what}")

    def signature_statuses(self, sigs, *, search_history=True):
        assert search_history is True and sigs == [SIG]
        self._chk("status")
        return [self._st.pop(0) if len(self._st) > 1 else self._st[0]], 1

    def transaction(self, sig, *, commitment="confirmed"):
        self._chk("tx")
        return self.tx

    def block_height(self, commitment="finalized"):
        assert commitment == "finalized"
        self._chk("height")
        return self.height

    def minimum_ledger_slot(self):
        self._chk("ledger")
        return self.ledger

    def is_blockhash_valid(self, h, *, commitment="processed"):
        self._chk("valid")
        return self.valid, 1


def two(**kw):
    return [Ep("a", **kw), Ep("b", **kw)]


# --- найдена -------------------------------------------------------------------------------------
def test_found_on_one_rpc_only_is_landed():              # X03: null первого узла ничего не значит
    r = resolve(ATT, [Ep("a"), Ep("b", statuses=[CONF], tx=TXOK)])
    assert (r.outcome, r.commitment, r.slot, r.amounts_known) == (Outcome.LANDED_OK, "confirmed", 120, True)


def test_landed_but_receipt_late_is_unknown_amount():
    r = resolve(ATT, [Ep("a", statuses=[CONF]), Ep("b")])
    assert r.outcome == Outcome.LANDED_OK and not r.amounts_known and "UNKNOWN_AMOUNT" in r.reason


def test_landed_err_and_finalized():
    r = resolve(ATT, two(statuses=[dict(FIN, err=ERR)], tx={"slot": 120, "meta": {"err": ERR}}))
    assert (r.outcome, r.final, r.err) == (Outcome.LANDED_ERR, True, ERR)


def test_expired_blockhash_but_receipt_found_is_landed():   # §8 п.8: blockhash истёк, чек есть
    r = resolve(ATT, two(statuses=[FIN], tx=TXOK, height=10_000))
    assert r.outcome == Outcome.LANDED_OK and r.final


@pytest.mark.parametrize("eps,word", [
    (lambda: [Ep("a", statuses=[CONF]), Ep("b", statuses=[dict(CONF, err=ERR)])], "расходятся"),
    (lambda: two(statuses=[CONF], tx={"slot": 120, "meta": {"err": ERR}}), "чек и статус"),
    (lambda: two(statuses=[CONF], tx={"slot": 121, "meta": {"err": None}}), "форк"),
    (lambda: two(statuses=[dict(CONF, confirmationStatus="processed")]), "processed"),
])
def test_contradictions_are_unknown(eps, word):
    r = resolve(ATT, eps())
    assert r.outcome == Outcome.UNKNOWN and word in r.reason


# --- не найдена ----------------------------------------------------------------------------------
def test_one_null_other_silent_is_unknown():             # X04
    r = resolve(ATT, [Ep("a"), Ep("b", fail={"status"})])
    assert r.outcome == Outcome.UNKNOWN and "не ответили" in r.reason


@pytest.mark.parametrize("ha,hb", [(400, 400), (501, 500), (500, 9999)])
def test_blockhash_may_still_land_is_unknown(ha, hb):     # X04: срок не вышел хотя бы у одного узла
    r = resolve(ATT, [Ep("a", height=ha), Ep("b", height=hb)])
    assert r.outcome == Outcome.UNKNOWN and "ещё может" in r.reason


def test_expired_proven_only_after_full_protocol():      # X05
    eps = two(height=501, ledger=50)
    r = resolve(ATT, eps)
    assert r.outcome == Outcome.EXPIRED_NOT_LANDED
    for ep in eps:                                        # порядок: статус → высота → история → статус → чек
        assert ep.calls == ["status", "height", "ledger", "status", "tx"]
    for k in ("status_a", "finalized_height", "minimum_ledger_slot", "status_b", "tx_finalized"):
        assert k in r.evidence
    assert r.evidence["finalized_height"] == {"a": 501, "b": 501}


@pytest.mark.parametrize("att,eps,word", [
    (ATT, lambda: [Ep("a", height=501)], "независимых"),                                   # один RPC
    (AttemptRef(SIG, BH, 500, True, 100, provider_pending=True), lambda: two(height=501), "провайдер"),
    (AttemptRef(SIG, BH, 999, False, 100), lambda: two(height=99_999), "G08"),             # чужая высота
    (AttemptRef(SIG, BH, None, False, 100), lambda: two(height=99_999), "G08"),
    (AttemptRef(SIG, BH, 500, True, None), lambda: two(height=501), "слота blockhash"),
    (ATT, lambda: two(height=501, ledger=150), "история узла"),                             # не покрывает
    (ATT, lambda: [Ep("a", height=501), Ep("b", height=501, fail={"height"})], "высота"),
    (ATT, lambda: [Ep("a", height=501), Ep("b", height=501, fail={"ledger"})], "истори"),
    (ATT, lambda: two(height=501, tx=TXOK), "расходятся"),                                  # чек без статуса
    (ATT, lambda: [Ep("a", height=501), Ep("b", height=501, fail={"tx"})], "не все"),
])
def test_expiry_not_proven_is_unknown(att, eps, word):
    r = resolve(att, eps())
    assert r.outcome == Outcome.UNKNOWN and word in r.reason, r.reason


def test_g08_records_is_blockhash_valid_only_as_evidence():
    r = resolve(AttemptRef(SIG, BH, 999, False, 100), two(height=99_999, valid=False))
    assert r.outcome == Outcome.UNKNOWN and r.evidence["is_blockhash_valid"] == {"a": False, "b": False}


def test_landing_seen_only_after_height_check():         # появилась между опросами — исход «в сети»
    r = resolve(ATT, [Ep("a", statuses=[None, CONF], height=501, tx=TXOK), Ep("b", height=501)])
    assert r.outcome == Outcome.LANDED_OK


# --- настоящий транспорт на записанных ответах mainnet ------------------------------------------
def test_recorded_mainnet_responses_through_rpc():
    sigs = fx_doc("statuses_history.json")["params"][0]
    status = fx("statuses_history.json")["result"]["value"][sigs.index(SIG)]
    ctx = fx("statuses_history.json")["result"]["context"]

    def statuses(p, rid):
        assert p[0] == [SIG] and p[1] == {"searchTransactionHistory": True}
        return {"context": ctx, "value": [status]}

    def gettx(p, rid):
        assert p[1]["maxSupportedTransactionVersion"] == 0 and p[1]["encoding"] == "json"
        return fx("tx_jup_sell.json")["result"]

    https = [FakeHttp({"getSignatureStatuses": statuses, "getTransaction": gettx}) for _ in range(2)]
    pool = RpcPool([SolanaRpc("https://rpc-a.test", name="primary", session=https[0]),
                    SolanaRpc("https://rpc-b.test", name="secondary", session=https[1])])
    r = resolve(AttemptRef(SIG, BH, None, False, None), pool.endpoints)
    assert (r.outcome, r.commitment, r.slot) == (Outcome.LANDED_OK, "finalized", SELL["slot"])
    assert parse_receipt(r.tx, expect_signature=SIG).ok
    assert https[0].methods() == ["getGenesisHash", "getSignatureStatuses", "getTransaction"]
    assert https[0].calls[-1][2][1]["commitment"] == "finalized"
    # без истории узел вернул бы null (записанный ответ statuses_recent) — резолвер без истории не спрашивает
    assert fx("statuses_recent.json")["result"]["value"] == [None] * 5
