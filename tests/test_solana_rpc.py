"""JSON-RPC Solana: сверка genesis, id ответа, дедлайн и повтор только чтений, sendTransaction ровно один раз и
только в live, ответ-ошибка узла не повторяется у соседа, ключ провайдера в URL не утекает, типизированные
чтения на записанных ответах mainnet."""
import pytest
import requests
from funding_bot.trade.keys import ModeForbidden
from funding_bot.trade.solana import ANSEM_MINT, TOKEN_2022_PROGRAM
from funding_bot.trade.solana.rpc import (BlockhashInfo, GenesisMismatch, RpcError, RpcPool, RpcUnavailable,
                                          SendUnknown, SolanaRpc, redact_url)
from solana_helpers import FakeHttp, Resp, fx, fx_doc, rpc_error

SIG = fx("tx_jup_sell.json")["result"]["transaction"]["signatures"][0]
DEVNET = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.sleeps.append(d)
        self.t += d


def mk(http, url="https://rpc-a.test/", **kw):
    clock = Clock()
    return SolanaRpc(url, session=http, clock=clock, sleep=clock.sleep, **kw), clock


def test_genesis_checked_once_then_cached():
    http = FakeHttp({"getSlot": 7})
    rpc, _ = mk(http)
    assert rpc.slot() == 7 and rpc.slot() == 7
    assert http.methods() == ["getGenesisHash", "getSlot", "getSlot"]


def test_foreign_network_blocked_forever():
    http = FakeHttp({"getSlot": 7}, genesis=DEVNET)
    rpc, _ = mk(http)
    for _ in range(2):
        with pytest.raises(GenesisMismatch):
            rpc.slot()
    assert http.methods() == ["getGenesisHash"]           # второй раз узел даже не спрашиваем


def test_reads_retry_transient_answers():
    http = FakeHttp(script=[lambda p, rid: Resp(429),
                            lambda p, rid: Resp(200, {"jsonrpc": "2.0", "id": rid + 100, "result": 1}),  # чужой id
                            rpc_error(-32005, "Node is behind"),
                            lambda p, rid: Resp(200, text="<html>"),
                            42])
    rpc, clock = mk(http, retries=4, backoff_s=(0.1,))
    assert rpc.block_height() == 42
    assert clock.sleeps == [0.1] * 4 and rpc.stats.n_retry == 4 and rpc.stats.n_fail == 4


def test_deadline_bounds_retries():
    clock = Clock()

    def slow(p, rid):
        clock.t += 6
        return Resp(503)

    http = FakeHttp(script=[slow] * 10)
    rpc = SolanaRpc("https://rpc-a.test", session=http, clock=clock, sleep=clock.sleep, budget_s=10, retries=9,
                    backoff_s=(1,))
    with pytest.raises(RpcUnavailable, match="HTTP 503"):
        rpc.slot()
    posts = [c for c in http.calls if c[1] == "getSlot"]
    assert len(posts) == 2 and posts[1][3] <= 3.0        # второй запрос — с остатком дедлайна, не полным таймаутом


def test_answered_error_and_auth_error_not_retried():
    http = FakeHttp(script=[rpc_error(-32602, "Invalid param: WrongSize")])
    rpc, clock = mk(http)
    with pytest.raises(RpcError) as e:
        rpc.transaction(SIG)
    assert e.value.code == -32602 and http.methods().count("getTransaction") == 1 and clock.sleeps == []
    http = FakeHttp(script=[lambda p, rid: Resp(401)])
    rpc, clock = mk(http)
    with pytest.raises(RpcUnavailable, match="401"):
        rpc.slot()
    assert http.methods().count("getSlot") == 1


def test_provider_key_in_url_never_leaks():
    url = "https://mainnet.helius-rpc.com/?api-key=SEKRETKEY123"
    boom = requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {url}")
    http = FakeHttp(script=[boom] * 3)
    rpc, _ = mk(http, url=url, backoff_s=(0,))
    with pytest.raises(RpcUnavailable) as e:
        rpc.slot()
    for text in (str(e.value), repr(rpc), rpc.label, rpc.stats.last_error):
        assert "SEKRETKEY123" not in text
    assert rpc.label == "primary@mainnet.helius-rpc.com"
    assert redact_url(url) == "https://mainnet.helius-rpc.com"
    assert redact_url("https://x.solana-mainnet.quiknode.pro/abcdef0123/") == "https://x.solana-mainnet.quiknode.pro"

    class SecretUrl:                                      # как keys.SecretUrl: значение только через reveal()
        def reveal(self):
            return "https://rpc.example.com/?api-key=SEKRET2"

    rpc2 = SolanaRpc(SecretUrl(), session=FakeHttp({"getSlot": 1}))
    assert rpc2.slot() == 1 and rpc2.label == "primary@rpc.example.com" and "SEKRET2" not in repr(rpc2)
    with pytest.raises(ValueError):
        SolanaRpc("ftp://rpc.example.com")


def test_send_is_single_shot_unknown_on_silence():
    http = FakeHttp(script=[requests.Timeout("read timed out")])
    rpc, clock = mk(http)
    with pytest.raises(SendUnknown):
        rpc.send_transaction("AAAA", expected_signature=SIG, mode="live")
    assert http.methods().count("sendTransaction") == 1 and clock.sleeps == []
    assert http.calls[-1][2][1] == {"encoding": "base64", "skipPreflight": False, "preflightCommitment": "confirmed"}
    http = FakeHttp(script=[lambda p, rid: Resp(503)])
    rpc, clock = mk(http)
    with pytest.raises(SendUnknown):
        rpc.send_transaction("AAAA", expected_signature=SIG, mode="live")
    assert http.methods().count("sendTransaction") == 1


def test_send_only_in_live():
    for mode in (None, "dry", "readonly"):
        http = FakeHttp({"sendTransaction": SIG})
        rpc, _ = mk(http)
        with pytest.raises(ModeForbidden):
            rpc.send_transaction("AAAA", expected_signature=SIG, mode=mode)
        assert http.methods() == []                       # ворота раньше любого сетевого вызова


def test_send_answers_are_evidence_not_outcome():
    rpc, _ = mk(FakeHttp(script=[SIG]))
    assert rpc.send_transaction("AAAA", expected_signature=SIG, mode="live", max_retries=0) == SIG
    other = fx("tx_jup_buy.json")["result"]["transaction"]["signatures"][0]
    rpc, _ = mk(FakeHttp(script=[other]))
    with pytest.raises(SendUnknown, match="другую подпись"):
        rpc.send_transaction("AAAA", expected_signature=SIG, mode="live")
    rpc, _ = mk(FakeHttp(script=[rpc_error(-32002, "Transaction simulation failed")]))
    with pytest.raises(RpcError):
        rpc.send_transaction("AAAA", expected_signature=SIG, mode="live")
    rpc, _ = mk(FakeHttp())
    with pytest.raises(ValueError):
        rpc.call("sendTransaction", ["AAAA"])
    with pytest.raises(ValueError):
        rpc.call("requestAirdrop", [ANSEM_MINT, 1])


def test_pool_failover_and_independent_answers():
    a = FakeHttp(script=[Resp(503)] * 3)
    b = FakeHttp({"getSlot": 9})
    pool = RpcPool([SolanaRpc("https://a.test", name="primary", session=a, sleep=lambda s: None, backoff_s=(0,)),
                    SolanaRpc("https://b.test", name="secondary", session=b)])
    assert pool.primary.label == "primary@a.test"
    assert pool.read(lambda ep: ep.slot()) == 9
    each = RpcPool([SolanaRpc("https://a.test", session=FakeHttp(script=[Resp(503)] * 3), sleep=lambda s: None,
                              backoff_s=(0,)),
                    SolanaRpc("https://b.test", name="secondary", session=FakeHttp({"getSlot": 9}))]
                   ).each(lambda ep: ep.slot())
    assert isinstance(each[0][1], RpcUnavailable) and each[1] == ("secondary@b.test", 9)
    a2, b2 = FakeHttp(script=[rpc_error(-32602, "bad")]), FakeHttp({"getSlot": 9})
    pool2 = RpcPool([SolanaRpc("https://a.test", session=a2), SolanaRpc("https://b.test", name="s", session=b2)])
    with pytest.raises(RpcError):                         # ответ по существу у соседа не переспрашивается
        pool2.read(lambda ep: ep.slot())
    assert "getSlot" not in b2.methods()
    with pytest.raises(ValueError):
        RpcPool([SolanaRpc("https://same.test", session=a), SolanaRpc("https://same.test/?api-key=x", name="s",
                                                                         session=b)])
    with pytest.raises(ValueError):
        RpcPool([])
    labels = [e.label for e in RpcPool.from_urls(["https://a.test", "", "https://b.test"]).endpoints]
    assert labels == ["primary@a.test", "secondary@b.test"]


def test_typed_reads_on_recorded_mainnet_answers():
    old_bh = fx("tx_jup_buy.json")["result"]["transaction"]["message"]["recentBlockhash"]
    sigs = fx_doc("statuses_history.json")["params"][0]

    def rent(p, rid):
        return fx(f"rent_{p[0]}.json")["result"]

    def statuses(p, rid):
        assert p == [sigs, {"searchTransactionHistory": True}]
        return fx("statuses_history.json")["result"]

    def gettx(p, rid):
        assert p[1] == {"encoding": "json", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}
        return fx("tx_jup_sell.json")["result"]

    http = FakeHttp({"getLatestBlockhash": fx("latest_blockhash.json")["result"],
                     "getBlockHeight": fx("block_height_finalized.json")["result"],
                     "isBlockhashValid": fx("is_blockhash_valid_old.json")["result"],
                     "minimumLedgerSlot": fx("minimum_ledger_slot.json")["result"],
                     "getMinimumBalanceForRentExemption": rent, "getSignatureStatuses": statuses,
                     "getTransaction": gettx, "getAccountInfo": fx("mint_ansem_jsonParsed.json")["result"]})
    rpc = SolanaRpc("https://rpc-a.test", session=http)
    assert rpc.latest_blockhash("finalized") == BlockhashInfo("6togmU26BronzVRBVZyf2Fok7SktZJgfrLRHdipaNaHR",
                                                              424_758_164, 446_715_360, "finalized",
                                                              "primary@rpc-a.test")
    assert rpc.block_height() == 424_758_004
    assert rpc.is_blockhash_valid(old_bh) == (False, 446_715_397)
    assert rpc.minimum_ledger_slot() == 446_641_608
    assert rpc.rent_exempt_lamports(170) == 1_513_840 and rpc.rent_exempt_lamports(170) == 1_513_840
    assert http.methods().count("getMinimumBalanceForRentExemption") == 1    # кэш по размеру
    vals, slot = rpc.signature_statuses(sigs)
    assert [v["confirmationStatus"] for v in vals] == ["finalized"] * 5 and slot == 446_715_371
    assert rpc.transaction(SIG)["transaction"]["signatures"][0] == SIG
    value, _ = rpc.account_info(ANSEM_MINT)
    assert value["owner"] == TOKEN_2022_PROGRAM
    with pytest.raises(ValueError):
        rpc.transaction(SIG, commitment="processed")
    with pytest.raises(ValueError):
        rpc.account_info("not-a-key")


def test_malformed_answers_are_unavailable():
    sigs = [SIG, SIG]
    for handlers, call in (
            ({"getSignatureStatuses": {"context": {"slot": 1}, "value": [None]}}, lambda r: r.signature_statuses(sigs)),
            ({"getBlockHeight": -1}, lambda r: r.block_height()),
            ({"getBlockHeight": True}, lambda r: r.block_height()),
            ({"getBlockHeight": "5"}, lambda r: r.block_height()),
            ({"getLatestBlockhash": {"value": {}}}, lambda r: r.latest_blockhash()),
            ({"isBlockhashValid": {"context": {"slot": 1}, "value": "no"}},
             lambda r: r.is_blockhash_valid("6togmU26BronzVRBVZyf2Fok7SktZJgfrLRHdipaNaHR")),
            ({"getSignatureStatuses": {"context": {"slot": 1}, "value": [{"slot": 1, "confirmationStatus": "x"}]}},
             lambda r: r.signature_statuses([SIG]))):
        rpc = SolanaRpc("https://rpc-a.test", session=FakeHttp(handlers))
        with pytest.raises(RpcUnavailable):
            call(rpc)


def test_health_reports_lag():
    rpc = SolanaRpc("https://rpc-a.test", session=FakeHttp(script=[rpc_error(-32005, "Node is behind",
                                                                             {"numSlotsBehind": 42})]))
    assert rpc.health() == (False, 42)
    assert SolanaRpc("https://rpc-a.test", session=FakeHttp(script=["ok"])).health() == (True, None)
