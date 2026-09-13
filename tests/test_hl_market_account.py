"""Поток hl: HlMarket/HlAccount на записанных публичных ответах (tests/data/hl) через подделку HTTP — точный рынок
(H01), смена метаданных (H02), маржа только в ledger para (H03), чтения по счёту, а не агенту (H04), readonly без
ключа (H05), неизвестное ≠ 0 (H19), режимы счёта (default/unified/standard), лимиты OI, часы."""
import copy
from decimal import Decimal as D
import pytest
import requests
from funding_bot.client import BudgetExceeded
from funding_bot.trade import hl_rules as R
from funding_bot.trade.hyperliquid_trade import (HlError, HlJournal, HlSigner, HyperliquidTrade, connect_journal)
from funding_bot.trade.keys import ModeForbidden
import hl_support as S

eth_account = pytest.importorskip("eth_account")
from eth_account import Account                                   # noqa: E402

A = "para:ANSEM"


def trade(account=S.MASTER, *, fake=None, clock=None, signer=False, tmp=None, mode="dry", **kw):
    clock = clock or S.Clock()
    fake = fake or S.FakeHL(clock)
    sg = jr = None
    if signer:
        sg = HlSigner(Account.from_key(S.TEST_KEY), agent=S.AGENT, master=account, account=account)
        jr = HlJournal(connect_journal(tmp / "hl.db"), now=clock)
    t = HyperliquidTrade(account, signer=sg, journal=jr, session=fake, now=clock, sleep=clock.sleep,
                         mode_state=lambda: (mode, False), **kw)
    return t, fake, clock


# --- рынок ------------------------------------------------------------------------------------------
def test_identity_uses_exact_dex_requests():
    t, fake, _ = trade()
    ref = t.identity()
    assert ref.asset == 180025
    assert fake.info_calls("meta") == [{"type": "meta", "dex": "para"}]
    assert fake.info_calls("perpDexs") == [{"type": "perpDexs"}]


def test_h01_book_exact_coin_and_decimals():
    t, fake, clock = trade()
    b = t.book(A, 5)
    assert fake.info_calls("l2Book") == [{"type": "l2Book", "coin": "para:ANSEM"}]
    assert b.bids[0] == (D("0.16675"), D("1062.0")) and b.asks[0][0] == D("0.16746") and len(b.bids) == 5
    assert all(isinstance(p, D) and isinstance(q, D) for p, q in b.bids + b.asks)
    assert b.ts == clock() and t.market.last_book_ms == S.PUB["fresh"]["l2Book"]["data"]["time"]
    with pytest.raises(ValueError, match="H01"):
        t.book("ANSEM")
    other = copy.deepcopy(S.PUB["fresh"]["l2Book"]["data"])
    other["coin"] = "ANSEM"
    fake.market["l2Book"] = other
    with pytest.raises(HlError, match="не по para:ANSEM"):
        t.book(A)
    crossed = copy.deepcopy(S.PUB["fresh"]["l2Book"]["data"])
    crossed["levels"][0][0]["px"] = "0.2"
    fake.market["l2Book"] = crossed
    with pytest.raises(HlError):
        t.book(A)


def test_funding_ctx_by_ordinal_and_next_hour():
    t, fake, clock = trade()
    mark, rate, nxt = t.funding(A)
    ctx = S.PUB["fresh"]["metaAndAssetCtxs_para"]["data"][1][25]
    assert (mark, rate) == (D(ctx["markPx"]), D(ctx["funding"]))
    assert nxt % 3_600_000 == 0 and 0 < nxt - clock() * 1000 <= 3_600_000
    c = t.market.ctx()
    assert c["oracle"] == D(ctx["oraclePx"]) and c["impact"] == (D(ctx["impactPxs"][0]), D(ctx["impactPxs"][1]))
    bad = copy.deepcopy(S.PUB["fresh"]["metaAndAssetCtxs_para"]["data"])
    bad[0]["universe"][24], bad[0]["universe"][25] = bad[0]["universe"][25], bad[0]["universe"][24]
    fake.market["metaAndAssetCtxs"] = bad
    with pytest.raises(R.HlIdentityChanged):
        t.market.ctx()


def test_funding_history_live_and_overlap_pages():
    t, fake, _ = trade()
    fh = S.PUB["fundingHistory"]
    p = t.market.funding_history(fh["payload"]["startTime"], fh["payload"]["endTime"])
    assert p.complete and len(p.rows) == 168 and p.pages == 1
    assert all(isinstance(r["rate"], D) for r in p.rows)
    rows = [{"coin": A, "fundingRate": "0.0001", "premium": "0.001", "time": 1786500000000 + i * 3_600_000}
            for i in range(720)]
    fake.market["fundingHistory"] = lambda b: [r for r in rows if b["startTime"] <= r["time"] <= b["endTime"]][:500]
    p = t.market.funding_history(rows[0]["time"], rows[-1]["time"])
    assert p.complete and len(p.rows) == 720 and p.pages == 2
    assert fake.info_calls("fundingHistory")[-1]["startTime"] == rows[499]["time"]       # включительно, без +1


def test_oi_state_cap_and_at_cap():
    t, fake, _ = trade()
    oi = t.market.oi_state()
    assert oi["cap_usd"] == D("5000000.0") and oi["at_cap"] is False
    assert oi["oi_usd"] == oi["open_interest"] * t.market.ctx()["mark"]
    fake.market["perpsAtOpenInterestCap"] = ["para:ANSEM"]
    assert t.market.oi_state()["at_cap"] is True


def test_filters_tick_is_derived_from_rule():
    t, _, _ = trade()
    f = t.filters(A)
    assert (f.tick, f.step, f.min_qty, f.min_notional) == (D("0.00001"), D(1), D(1), D(10))
    assert f.max_qty_limit == D("10000000000.0")
    assert t.quantize_px(A, D("0.158381"), "SELL") == D("0.15839")
    assert t.floor_qty(A, D("1234.56789")) == D(1234)


def test_check_clock_against_exchange_status():
    t, fake, clock = trade()
    assert abs(t.check_clock()) < 0.01
    fake.market["exchangeStatus"] = lambda b: {"specialStatuses": None, "time": int(clock() * 1000) + 3000}
    with pytest.raises(HlError, match="часы"):
        t.check_clock()


# --- счёт -------------------------------------------------------------------------------------------
def test_h04_reads_by_trading_account_never_agent(tmp_path):
    t, fake, _ = trade(S.U0, signer=True, tmp=tmp_path)
    assert t.position(A) == D("-63165.0")
    t.margin()
    t.acct.active_asset_data()
    t.fills_since(A, S.PUB["user_0"]["userFillsByTime"]["payload"]["startTime"])
    users = {p["user"] for p in fake.info_calls() if "user" in p}
    assert users == {S.U0.lower()} and S.AGENT.lower() not in users


def test_h19_unknown_is_none_never_zero():
    t, fake, clock = trade()
    fake.user(S.MASTER, "clearinghouseState", S.ch([]))
    assert t.position(A) == 0                                             # полный ответ без строки рынка
    fake.user(S.MASTER, "clearinghouseState", S.ch([("para:ANSEM", "-10.0")]))
    assert t.position(A) == D(-10)
    fake.user(S.MASTER, "clearinghouseState", S.ch([("xyz:TSLA", "1.0")]))   # ответ не того dex
    assert t.position(A) is None
    fake.user(S.MASTER, "clearinghouseState", {"marginSummary": {}, "withdrawable": "0.0"})
    assert t.position(A) is None
    fake.user(S.MASTER, "clearinghouseState", S.Resp(500, raw=b"oops"))
    assert t.position(A) is None
    fake.user(S.MASTER, "clearinghouseState", requests.exceptions.ConnectionError("down"))
    assert t.position(A) is None
    two = S.ch([("para:ANSEM", "-1.0"), ("para:ANSEM", "-2.0")])
    fake.user(S.MASTER, "clearinghouseState", two)
    assert t.position(A) is None
    fake.user(S.MASTER, "userAbstraction", "disabled")
    fake.user(S.MASTER, "clearinghouseState", S.Resp(500, raw=b"oops"))
    m = t.margin()
    assert m.available is None and t.available_margin() is None


def test_h05_readonly_without_keys_reads_and_refuses_sends():
    t, fake, _ = trade(S.U0)                                 # без подписанта и журнала, режим dry
    assert t.position(A) == D("-63165.0")
    assert t.margin().available == D("21061.845095")
    assert t.fills_since(A, S.PUB["user_0"]["userFillsByTime"]["payload"]["startTime"]).rows
    with pytest.raises(ModeForbidden):
        t.ioc(A, "SELL", D(100), D("0.16"), "c1", False)
    with pytest.raises(ModeForbidden):
        t.setup(A, 1, "ISOLATED")
    with pytest.raises(ModeForbidden):
        t.noop()
    t2, fake2, _ = trade(S.U0, mode="live")                 # live, но ключа нет — всё равно запрет
    with pytest.raises(ModeForbidden, match="readonly"):
        t2.ioc(A, "SELL", D(100), D("0.16"), "c1", False)
    assert fake.exchange_calls() == [] and fake2.exchange_calls() == []


def test_h03_main_dex_usdc_is_not_para_margin():
    t, fake, _ = trade()
    fake.user(S.MASTER, "userAbstraction", "disabled")
    main = S.ch([], withdrawable="10000.0")
    para = S.ch([], withdrawable="0.0")
    fake.user(S.MASTER, "clearinghouseState", lambda b: para if b.get("dex") == "para" else main)
    fake.user(S.MASTER, "spotClearinghouseState", {"balances": [{"coin": "USDC", "token": 0, "total": "5000.0",
                                                                "hold": "0.0", "entryNtl": "0.0"}]})
    out = t.entry_margin_refusals(D(30), D(5))
    assert len(out) == 1 and "0.0 USDC" in out[0] and "withdrawable" in out[0]
    assert all(p.get("dex") == "para" for p in fake.info_calls("clearinghouseState"))
    assert fake.info_calls("spotClearinghouseState") == []
    assert any("резерв" in x for x in t.entry_margin_refusals(D(30), None))
    fake.user(S.MASTER, "clearinghouseState", S.ch([], withdrawable="100.0"))
    assert t.entry_margin_refusals(D(30), D(5)) == []
    with pytest.raises(ValueError):
        t.entry_margin_refusals(30, D(5))


def test_modes_default_and_unified_live_fixtures():
    t0, _, _ = trade(S.U0)
    m0 = t0.margin()
    assert (m0.mode, m0.available, m0.trade_supported) == ("default", D("21061.845095"), False)
    assert any("не поддержан" in x for x in t0.entry_margin_refusals(D(30), D(5)))
    t1, f1, _ = trade(S.U1)
    m1 = t1.margin()
    assert (m1.mode, m1.available, m1.trade_supported) == ("unified", D("19787.61012389"), False)
    assert f1.info_calls("clearinghouseState") == []                     # у Unified маржа — в spot state
    # живая сверка: у unified-адреса para.withdrawable = 0, а доступно к торговле ≈ spot после маржи
    assert D(S.PUB["user_1"]["clearinghouseState_para"]["data"]["withdrawable"]) == 0
    assert t1.acct.active_asset_data()["available_to_trade"][1] == D("19787.47372")


def test_account_reads_fees_agents_leverage():
    t, fake, clock = trade(S.U0)
    fees = t.acct.user_fees()
    assert fees["cross"] == D("0.00045") and fees["referral_discount"] == 0
    assert R.taker_rate_estimate(fees["cross"], t.identity()) == D("0.000675")
    st = t.acct.agent_status(S.U0, "0x88a341461d439964cd2394690e237d621d07b9d9")
    assert st["listed"] and st["name"] == "ansem-mm" and st["expired"] is False
    assert t.acct.agent_status(S.U0, S.AGENT)["listed"] is False
    aad = t.acct.active_asset_data()
    assert (aad["leverage_type"], aad["leverage"], aad["mark"]) == ("isolated", 3, D("0.166338"))
    fake.user(S.U0, "activeAssetData", {**S.PUB["user_0"]["activeAssetData"]["data"], "user": S.U1})
    with pytest.raises(HlError, match="другому адресу"):
        t.acct.active_asset_data()


def test_h02_saved_identity_blocks_send_before_network(tmp_path):
    t0, _, _ = trade()
    saved = t0.identity()
    t, fake, _ = trade(signer=True, tmp=tmp_path, mode="live", saved_identity=saved)
    moved = copy.deepcopy(S.PUB["fresh"]["perpDexs"]["data"])
    moved[8], moved[9] = moved[9], moved[8]
    fake.market["perpDexs"] = moved
    with pytest.raises(R.HlIdentityChanged):
        t.ioc(A, "SELL", D(100), D("0.16"), "c-h02", False)
    assert fake.exchange_calls() == []
    assert t.journal.con.execute("SELECT COUNT(*) FROM hl_nonces").fetchone()[0] == 0


def test_read_budget_and_429_backoff():
    t, fake, clock = trade(weight_limit=100)
    t.identity()                                        # 2 × 20 = 40 из 50 (SOFT_READ)
    with pytest.raises(BudgetExceeded):
        t.market.annotation()
    clock.sleep(61)
    fake.market["perpAnnotation"] = S.Resp(429, raw=b"slow down", headers={"Retry-After": "7"})
    with pytest.raises(BudgetExceeded, match="429"):
        t.market.annotation()
    assert t.http.backoff_until == pytest.approx(clock() + 7)
    with pytest.raises(BudgetExceeded, match="пауза"):
        t.market.annotation()
